"""図の検出と切り出し範囲の決定（figures.json を作る）。

方針（契約 docs/contracts_v1.md の figures.json）:
1. キャプションを「行」単位で探す。本文より小さいフォントで `Fig. N.` `Table N.` 等から始まる行。
   本文サイズ(8.0pt)で `Fig. 5 presents ...` と始まる行は本文の言及なので除外する。
   サイズはハードコードせず、文書で最多の span サイズを本文サイズとする。
2. キャプションの上の領域（同じ段、または全幅）を、直近の本文行の下端〜キャプション上端で区切り、
   その中の描画（page.get_drawings）・画像の和集合に縮める。図中の文字はアウトライン化された
   パスなので、描画の和集合で文字ごと取れる。
3. 上で何も取れない/小さすぎるときは「キャプションの下」を試す（表は下が基本）。
4. 空・小さい・本文と重なる場合は confidence を low にする。
5. figures_override.json があれば id で上書きする（source: "override"）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pymupdf

from .spanning import CONT_MARK_RE, continued_blocks, continued_captions, widen_cut
from .orient import figure_view, rotated_captions, rotated_figures
from .pdfutil import (CAPTION_RE, Line, _build_line, all_lines, body_font_size, column_of,
                      in_margin, is_body, is_small, margin_bands, overlap_size)

TABLE_BOLD_RE = re.compile(r"^\s*Tables?\s*(S?\d+|(?-i:[IVX]+))\s*[.:|]", re.I)
UPPER_CAPTION_RE = re.compile(r"^\s*(FIGURE|TABLE)\s+\d+\b")
PAD = 4.0            # 描画の和集合に足す余白 (pt)
MIN_HEIGHT = 40.0    # これ未満の高さは「小さすぎ」で low
# ただし横長の図（幅が高さの 4 倍以上・高さ 20pt 以上）は低くても図とみなす。
# Nomura 2025 Fig.4 は高さ 23pt・幅 181pt の模式図で、40pt 未満として捨てられ、下の Fig.5 の絵を取った
MIN_HEIGHT_WIDE = 20.0
ROMAN = {"I": 1, "V": 5, "X": 10}


def _arabic(number: str) -> str:
    """番号をアラビア数字の文字列にする（"II" → "2"、"S3" はそのまま）。id と並べ替えをアラビア数字でそろえる。"""
    if not number or not set(number) <= set(ROMAN):
        return number
    vals = [ROMAN[c] for c in number]
    return str(sum(-v if i + 1 < len(vals) and v < vals[i + 1] else v for i, v in enumerate(vals)))


def _big_enough(clip) -> bool:
    """図として取ってよい大きさか（MIN_HEIGHT 以上、または横長で MIN_HEIGHT_WIDE 以上）。"""
    h, w = clip[3] - clip[1], clip[2] - clip[0]
    return h >= MIN_HEIGHT or (h >= MIN_HEIGHT_WIDE and w >= 4 * h)
# 柱（ヘッダー）とページ番号の帯はページの高さに比例させる（pdfutil.margin_bands。Elsevier 792pt で 48/750）


def _kind_of(word: str) -> str:
    w = word.lower().rstrip(".")
    return {"fig": "figure", "figure": "figure", "scheme": "scheme", "table": "table"}[w]


def _fig_id(word: str, number: str) -> str:
    """id は番号から安定に作る。Fig→fig、Scheme→scheme、Table→table。"""
    w = word.lower().rstrip(".")
    prefix = {"fig": "fig", "figure": "fig", "scheme": "scheme", "table": "table"}[w]
    return f"{prefix}{number}"


def _label_of(word: str, number: str) -> str:
    w = word.lower().rstrip(".")
    return {"fig": "Fig.", "figure": "Fig.", "scheme": "Scheme", "table": "Table"}[w] + " " + number


def _font_of(ln) -> str:
    """行の最初の（空白でない）span のフォント名。"""
    return next((s["font"] for s in ln["spans"] if s["text"].strip()), "")


LABEL_ONLY_RE = re.compile(r"^\s*(Fig\.|Figure|Scheme|Table)\s*(S?\d+|(?-i:[IVX]+))\s*[.:|]?\s*$", re.I)


def _join_label_row(ln, lines):
    """札だけの行（"Fig. 6"）に、同じ高さで右に続くキャプション本文の行をつなぎ、(つないだ行, 使った行) を返す。
    RSC（Sapouna 2021 Fig.6/8）では本文側の上付き "H+" で行の上端が 3.3pt 上がり、pdfutil._merge_same_row
    （上端・下端とも差 2pt 未満）でつながらず、キャプションが札だけ＝幅 19pt で左段と判定され、右のパネルを
    切り落としていた。全部の行の判定を緩めると表の隣のセルなどまでつなぐので、札だけの行に限る（Codex 2026-10-31）。
    条件: 右隣（隙間 -1〜12pt）・同じ字の大きさ・縦の重なりが低い方の 70% 以上・下端の差 2pt 未満。候補が 1 つのときだけ。"""
    if not LABEL_ONLY_RE.match(ln["text"]):
        return ln, None
    cands = []
    for c in lines:
        if c is ln or not (-1 <= c.x0 - ln.x1 < 12) or abs(c["size"] - ln["size"]) > 0.35:
            continue
        overlap = min(ln.y1, c.y1) - max(ln.y0, c.y0)
        if overlap >= 0.7 * min(ln.y1 - ln.y0, c.y1 - c.y0) and abs(ln.y1 - c.y1) < 2:
            cands.append(c)
    if len(cands) != 1:
        return ln, None
    c = cands[0]
    spans = list(ln["spans"]) + [dict(ln["spans"][-1], text=" ")] + list(c["spans"])
    return _build_line(ln["page"], (ln.x0, min(ln.y0, c.y0), c.x1, max(ln.y1, c.y1)), spans), c


def _find_captions(doc, lines_by_page, body: float) -> list[dict]:
    """本文より小さい行で図表キャプションが始まる行を探し、続く行を連結してキャプションを組む。"""
    found: dict[str, dict] = {}
    for pno, lines in enumerate(lines_by_page, 1):
        pw = doc[pno - 1].rect.width
        for ln in lines:
            m = CAPTION_RE.match(ln["text"])
            if not m:
                continue
            ln, joined = _join_label_row(ln, lines)
            # ACS の表題は本文と同じ大きさ（Hololignin・Jia 2017。太字だがフォント名が暗号化され太字と判定できない）。
            # 表に限り「Table N. と句点まで書かれ、すぐ下（か上）に表の罫線がある行」なら題とみなす
            # （図は小さい字のキャプションだけ。本文の言及を図の題と取り違えて、図の切り出しが変わらないように）
            by_bold = (not is_small(ln, body) and m.group(1).lower().startswith("table")
                       and TABLE_BOLD_RE.match(ln["text"])
                       and _table_bbox(doc[pno - 1], {"bbox": [ln.x0, ln.y0, ln.x1, ln.y1]}) is not None)
            # Wiley の新しい組版（Takada 2019）は題が本文と同じ大きさで「FIGURE 1」「TABLE 1 The yield…」と全部大文字で書く
            # （本文の言及は "Figure 1" なので取り違えない）
            by_upper = UPPER_CAPTION_RE.match(ln["text"]) is not None
            if not (is_small(ln, body) or by_bold or by_upper):
                continue
            # 余白の帯は実際のページの高さで決める（0 を渡すと 792pt 扱いになり、A4＝842pt の論文では
            # y>750 の最下段のキャプションを捨てていた。Miyamoto 2025 JJAP の Fig.6/8/11/13）
            if in_margin(ln, doc[pno - 1].rect.height) or CONT_MARK_RE.search(ln["text"]):
                continue
            word, roman = m.group(1), m.group(2)
            number = _arabic(roman)             # "Table II" → id table2・番号 2（札は原文の "Table II" のまま）
            fid = _fig_id(word, number)
            if fid in found:        # 同じ id が二度出たら最初のものを採用（二度目は参考文献などの可能性）
                continue
            layout = column_of(ln.x0, ln.x1, pw)
            chain = [ln]
            prev = ln
            while True:
                nxt = None
                for c in lines:
                    if c is prev or c is joined or c in chain:
                        continue
                    if abs(c["size"] - ln["size"]) > 0.35 or CAPTION_RE.match(c["text"]):
                        continue
                    if by_bold and _font_of(c) != _font_of(ln):
                        continue        # 本文と同じ大きさの題: 続きの行も題と同じフォントだけ（本文の行を題に取り込まない）
                    # 直下の行（行間が詰まっている・同じ行は除く）
                    if not (c.y0 > prev.y0 + 4 and c.y0 - prev.y1 < 5):
                        continue
                    cl = column_of(c.x0, c.x1, pw)
                    if layout == "left" and cl != "left":
                        continue
                    if layout == "right" and cl != "right":
                        continue
                    # 続きの行はキャプションの左端にそろう。そろわない行は図の中の文字
                    # （Springer の横置きキャプションの右にある軸の目盛り "1.8x10-11" などを拾っていた）
                    if abs(c.x0 - ln.x0) > 6:
                        continue
                    # 表の題の直下で同じ高さに別のセルが並ぶ行は表の見出し（Huang 2024 Table 7 の "Stage"、Wang 2024 Table 2）
                    if (_kind_of(word) == "table" and any(o is not c and abs(o.y0 - c.y0) < 3 and c.x1 < o.x0 < ln.x1
                                                          for o in lines)):
                        continue
                    if nxt is None or c.y0 < nxt.y0:
                        nxt = c
                if nxt is None:
                    break
                chain.append(nxt)
                prev = nxt
            x0 = min(c.x0 for c in chain); y0 = min(c.y0 for c in chain)
            x1 = max(c.x1 for c in chain); y1 = max(c.y1 for c in chain)
            found[fid] = {
                "id": fid, "number": number, "label": _label_of(word, roman),
                "kind": _kind_of(word), "page": pno, "chain": chain,
                "bbox": [x0, y0, x1, y1], "layout": layout,
            }
    return list(found.values())


def _join_caption(chain: list[Line]) -> str:
    """キャプション行を 1 本の文字列にする（改行は空白、行末のソフトハイフンは除去）。"""
    s = ""
    for c in chain:
        t = c["text"].rstrip()
        if s.endswith("­"):
            s = s[:-1] + t.lstrip()
        elif s.endswith("-") and s[-2:-1].isalpha():
            s = s + t.lstrip()
        else:
            s = (s + " " + t.lstrip()).strip()
    return re.sub(r"\s+", " ", s.replace("­", ""))


def _col_range(layout: str, pw: float) -> tuple[float, float]:
    mid = pw / 2
    if layout == "left":
        return (0.0, mid + 3)
    if layout == "right":
        return (mid - 3, pw)
    return (0.0, pw)


def _ink_bbox(page, region, scale: float = 2.0, thresh: int = 245):
    """region（pt）をグレースケールで描画し、白でない画素の外接矩形(pt)を返す。無ければ None。

    page.get_drawings() の rect は、クリップされた巨大な複合パス（Excel/Illustrator 由来で
    数千要素）だと実際の見た目より大きく出る（Fig. 3 で右に 90pt、Fig. 11 で左に 15pt はみ出した）。
    描画結果（クリップ・不可視の白塗りを反映済み）で測れば、見た目どおりの範囲になる。
    図中の文字はアウトライン化パスなので、文字・画像も一緒にこれで取れる。"""
    import numpy as np
    rect = pymupdf.Rect(*region)
    if rect.width < 2 or rect.height < 2:
        return None
    # annots=False: Mendeley で付けたハイライト・手書きの注釈を「図の墨」に数えない
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect,
                          colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    ys, xs = np.where(arr < thresh)
    if len(xs) == 0:
        return None
    # pixmap の左上は clip の左上（丸めの分だけずれるので pix.x/pix.y ではなく rect を基準にする）
    sx = rect.width / pix.width
    sy = rect.height / pix.height
    return (float(rect.x0 + xs.min() * sx), float(rect.y0 + ys.min() * sy),
            float(rect.x0 + (xs.max() + 1) * sx), float(rect.y0 + (ys.max() + 1) * sy))


def paragraph_lines(lines, body: float, ph: float, typical_w: float) -> list[Line]:
    """本文の段落の行だけを返す。図の中に本文と同じ大きさの文字（実テキストのラベル）があっても境界にしない。

    Elsevier の図中の文字は輪郭の塗りなので問題にならなかったが、Springer の機構図は "Monomers" "Char" などが
    本文サイズの文字で、図の上端が最下段のラベルの下に来て 14pt の帯しか取れなかった（Kawamoto 2017 Fig. 8）。
    段落の行 = 本文サイズで、幅が典型の行幅の半分以上。短い行でも、幅広の段落の行と左端がそろって
    すぐ上下に接していれば段落の最終行・見出しとみなす。"""
    cand = [l for l in lines if is_body(l, body) and not in_margin(l, ph)]
    wide = [l for l in cand if l.x1 - l.x0 >= 0.5 * typical_w]
    out = list(wide)
    for l in cand:
        if l in out:
            continue
        for w in wide:
            gap = l.y0 - w.y1 if l.y0 >= w.y0 else w.y0 - l.y1
            if abs(l.x0 - w.x0) <= 3 and -1 <= gap < 8:
                out.append(l)
                break
    return out


def running_bands(doc, lines_by_page) -> tuple[float, float]:
    """柱（ヘッダー）の帯の下端とページ番号の帯の上端（pt）。

    判型の比（pdfutil.margin_bands）に加えて、ページ上部・下部 12% にあって 2 ページ以上で同じ文字列
    （数字は同一視）の行を柱・ページ番号とみなし、その外側を帯に含める。ACS（818pt）の柱 "Energy & Fuels"
    は y=57.8 まで下りていて、比だけの帯（49.6）では図の切り出しに柱が入った。
    同じ文字列でも高さが揃っていない行は数えない。RSC は 1 ページ目の大きな誌名 "Green Chemistry"（下端 70.8）が
    2 ページ目以降の柱（下端 35）と同じ文字列で、帯が 71.8 まで広がって全ページの図の上端が切れた。"""
    ph = doc[0].rect.height if len(doc) else 792.0
    head, foot = margin_bands(ph)
    # 候補はページで一番上の行・一番下の行（同じ高さ ±3pt）だけ。図の中の "OH" なども上部で繰り返すため
    seen: dict[tuple[str, str], list[tuple[int, float]]] = {}
    cands = []
    for pno, lines in enumerate(lines_by_page):
        # 余白の縦書き（Wiley の "Downloaded from …" は y=16〜783）を一番上・一番下の行に数えない
        lines = [l for l in lines if l.y1 - l.y0 < 3 * max(l.x1 - l.x0, 1) or l.y1 - l.y0 < 30]
        if not lines:
            continue
        top_y = min(l.y0 for l in lines)
        bot_y = max(l.y1 for l in lines)
        for l in lines:
            key = re.sub(r"\d+", "#", l["text"].strip())
            if len(key) < 2:
                continue
            if l.y0 <= top_y + 3 and l.y1 < ph * 0.12:
                cands.append(("h", key, l))
            # 下は 2 行の柱（Wiley の "© 2013 The Authors" ＋誌名の行）があるので下から 15pt まで
            elif l.y1 >= bot_y - 15 and l.y0 > ph * 0.88:
                cands.append(("f", key, l))
            else:
                continue
            seen.setdefault(cands[-1][:2], []).append((pno, l.y1))
    for side, key, l in cands:
        # 同じ文字列が同じ高さ（±3pt）で 2 ページ以上にあるときだけ柱・ページ番号とみなす
        if len({p for p, y in seen[(side, key)] if abs(y - l.y1) <= 3}) < 2:
            continue
        if side == "h":
            head = max(head, l.y1 + 1)
        else:
            foot = min(foot, l.y0 - 1)
    return head, foot


def typical_line_width(lines_by_page, body: float) -> float:
    """本文サイズの行の幅の 80 パーセンタイル（＝段の幅の目安）。"""
    ws = sorted(l.x1 - l.x0 for lines in lines_by_page for l in lines if is_body(l, body))
    return ws[int(len(ws) * 0.8)] if ws else 200.0


def _search_region(cap, paras, others, pw: float, ph: float, where: str, bands=None):
    """キャプションの上（above）・下（below）・右（right）の探索領域を返す。

    境界 = 同じ段にある段落の行 / 他のキャプション / 柱・ページ番号帯。
    right は Springer の横置きキャプション（左の細い段にキャプション、その右に図）。
    """
    head_y, foot_y = bands or margin_bands(ph)
    bx0, by0, bx1, by1 = cap["bbox"]
    # 横置きの図の外側の端は、そのページの本文の左端・右端（Wiley は奇数・偶数ページで余白が違い、対称とみなすと図が欠けた）
    lm = min((l.x0 for l in paras), default=pw - bx1) - 4
    rm = max((l.x1 for l in paras), default=pw - bx0) + 4
    if where == "right":
        cx0, cx1 = bx1 + 1, max(rm, pw - bx0)
    else:
        cx0, cx1 = _col_range(cap["layout"], pw)

    def in_col(l) -> bool:
        w, _ = overlap_size((l.x0, l.y0, l.x1, l.y1), (cx0, 0, cx1, ph))
        return w > 5

    bodies = [l for l in paras if in_col(l)]
    if where == "right":
        bottom = foot_y
        for l in bodies:
            if l.y0 >= by0 and l.y0 < bottom:
                bottom = l.y0
        for o in others:
            if o["page"] == cap["page"] and o["id"] != cap["id"] and o["bbox"][1] > by0:
                ow, _ = overlap_size(o["bbox"], (cx0, 0, cx1, ph))
                if ow > 5 and o["bbox"][1] < bottom:
                    bottom = o["bbox"][1]
                # 同じ左の細い段にある次のキャプション＝次の図の始まり（図の上端はキャプションとそろう）。
                # 右の範囲と横に重ならないので上では止まらず、Wang 2024 Fig.4 が下の Fig.5 まで伸びた
                cw, _ = overlap_size(o["bbox"], (bx0, 0, bx1, ph))
                if cw > 5 and o["bbox"][1] > by1 and o["bbox"][1] - 2 < bottom:
                    bottom = o["bbox"][1] - 2
        return (cx0, max(head_y, by0 - 4), cx1, bottom)
    if where == "left":
        # Wiley（Plant J）: 右の細い段にキャプション、その左に上端をそろえて図
        cx0, cx1 = max(0.0, min(lm, pw - bx1)), bx0 - 1
        bodies = [l for l in paras if overlap_size((l.x0, l.y0, l.x1, l.y1), (cx0, 0, cx1, ph))[0] > 5]
        top = max(head_y, by0 - 15)
        for l in bodies:
            if l.y1 + 2 <= by0 + 1 and l.y1 + 2 > top:
                top = l.y1 + 2             # 行の外接矩形より字の下端がはみ出す分（2pt）を空ける
        bottom = foot_y
        for l in bodies:
            if l.y0 >= by0 - 1 and l.y0 - 2 < bottom:
                bottom = l.y0 - 2
        for o in others:
            # 下の図のキャプション（同じく右の段）・ページ幅の表題で止める。図の上端はキャプションとそろうので少し手前
            if o["page"] == cap["page"] and o["id"] != cap["id"] and o["bbox"][1] > by1:
                ow, _ = overlap_size(o["bbox"], (cx0, 0, pw, ph))
                if ow > 5 and o["bbox"][1] - 6 < bottom:
                    bottom = o["bbox"][1] - 6
        return (cx0, top, cx1, bottom)
    if where == "above":
        top = head_y
        for l in bodies:
            # 上の本文の行の字の下端（外接矩形から 2pt ほどはみ出す）が写り込まないよう 2pt 空ける（Du 2013 Fig.2）
            if l.y1 <= by0 + 1 and l.y1 + 2 > top:
                top = l.y1 + 2
        for o in others:
            if o["page"] == cap["page"] and o["id"] != cap["id"] and o["bbox"][3] <= by0 + 1:
                ow, _ = overlap_size(o["bbox"], (cx0, 0, cx1, ph))
                if ow > 5 and o["bbox"][3] > top:
                    top = o["bbox"][3]
        return (cx0, top, cx1, by0 - 0.5)
    bottom = foot_y
    for l in bodies:
        if l.y0 >= by1 - 1 and l.y0 < bottom:
            bottom = l.y0
    for o in others:
        if o["page"] == cap["page"] and o["id"] != cap["id"] and o["bbox"][1] >= by1 - 1:
            ow, _ = overlap_size(o["bbox"], (cx0, 0, cx1, ph))
            if ow > 5 and o["bbox"][1] < bottom:
                bottom = o["bbox"][1]
    return (cx0, by1 + 0.5, cx1, bottom)


def _make_clip(cap, page, paras, others, where, bands=None):
    """where=above/below/right で clip と状態を作る。(clip|None, region) を返す。"""
    pw, ph = page.rect.width, page.rect.height
    region = _search_region(cap, paras, others, pw, ph, where, bands)
    if region[3] - region[1] < 8:
        return None, region
    # region は柱・ページ番号帯の内側に収めてあるので、ink（白以外）の外接矩形がそのまま図の本体
    u = _ink_bbox(page, region)
    if u is None:
        return None, region
    clip = (max(u[0] - PAD, region[0]), max(u[1] - PAD, region[1]),
            min(u[2] + PAD, region[2]), min(u[3] + PAD, region[3]))
    return clip, region


def _table_bbox(page, cap) -> tuple | None:
    """キャプションのすぐ下（無ければ上）にある表の外枠（pymupdf の find_tables）。
    表は段の幅を越えたり、本文と同じ大きさの字で書かれたりして、図と同じ探し方では欠ける
    （Huang 2024 Table 3 は右と上が切れた）。縦に続く表（2 つに割れて見つかる）はまとめる。"""
    try:
        tabs = [tuple(t.bbox) for t in page.find_tables().tables]
    except Exception:
        return None
    bx0, by0, bx1, by1 = cap["bbox"]

    def near(t):
        x0, y0, x1, y1 = t
        if x1 < bx0 - 20 or x0 > bx1 + 20 and x0 > bx0 + 300:
            return None                           # 横にずれすぎ（別の段の表）
        if y0 >= by1 - 2:
            return y0 - by1                       # 下
        if by0 - 5 <= y0 <= by1 and y1 > by1 + 10:
            return 0                              # 題の行まで表の 1 行目に含めて見つかった（Huang 2024 Table 3）
        if y1 <= by0 + 2:
            return by0 - y1 + 1000                # 上（下を優先する）
        return None
    cands = [(near(t), t) for t in tabs if near(t) is not None and (near(t) % 1000) < 40]
    if not cands:
        return None
    best = list(min(cands)[1])
    if best[1] < by1 <= best[3] and best[1] >= by0 - 5:
        # 題の行が表の外枠に入った。題は別に出すので外枠から外す
        if bx1 - bx0 < 0.4 * (best[2] - best[0]) and bx0 <= best[0] + 10:
            # Springer: 題が表の左の細い段にあり、表の見出しが題と同じ高さに並ぶ（Huang 2024 Table 3）。
            # 上端を下げると見出しが切れるので、左端を題の右に寄せる
            best[0] = bx1 + 2
        else:
            best[1] = by1 + 1
    grown = True
    while grown:                                  # 縦に続く表をまとめる
        grown = False
        for t in tabs:
            if t[1] <= best[3] + 15 and t[3] > best[3] and t[0] < best[2] and t[2] > best[0]:
                best = [min(best[0], t[0]), best[1], max(best[2], t[2]), t[3]]
                grown = True
    return (best[0] - PAD, best[1] - PAD, best[2] + PAD, best[3] + PAD)


def _confidence(clip, paras) -> str:
    """clip が空・小さい・段落の行と重なる場合は low。"""
    if clip is None:
        return "low"
    if not _big_enough(clip):
        return "low"
    for l in paras:
        w, h = overlap_size(clip, l["bbox"])
        if w > 3 and h > 3:
            return "low"
    return "high"


def detect_figures(doc) -> list[dict]:
    """PDF から図表の一覧を作る（figures.json の中身）。ページ順・番号順。"""
    body = body_font_size(doc)
    lines_by_page = all_lines(doc)
    caps = _find_captions(doc, lines_by_page, body)
    # 向きの違う図表は orient.py で別に扱う（ここで横書きとして組んだ同じキャプションは外す）
    rot_caps = rotated_captions(doc, body)
    rot_keys = {(c["id"], c["orig_page"]) for c in rot_caps}
    caps = [c for c in caps if (c["id"], c["page"]) not in rot_keys]
    rot_caps = [c for c in rot_caps if c["id"] not in {n["id"] for n in caps}]
    typ_w = typical_line_width(lines_by_page, body)
    bands = running_bands(doc, lines_by_page)
    heights = [p.rect.height for p in doc]
    # 探索の境界: キャプション＋続きの表＋表の本体。表の本体（小さい字の表）は段落の行に数えないので、
    # 境界にしないと同じページの図が表を抱き込む（Yao 2024 Fig.8 が上の Table 1 を、Fig.9 が Table 2 を取り込んだ）
    bodies = []
    for cap in caps:
        if cap["kind"] == "table":
            tb = _table_bbox(doc[cap["page"] - 1], cap)
            if tb is not None:
                # 表の脚注（"nd, not detected; …"）は外枠の下に小さい字で続く。行間 10pt 未満の行を足して境界に含める
                x0, y0, x1, y1 = tb
                grown = True
                while grown:
                    grown = False
                    for c in lines_by_page[cap["page"] - 1]:
                        if y1 - 1 < c.y0 < y1 + 10 and c.y1 > y1 and c.x1 > x0 and c.x0 < x1 and is_small(c, body):
                            y1, grown = c.y1, True
                bodies.append({"id": f"{cap['id']}-body", "kind": "table-body", "page": cap["page"],
                               "bbox": [x0, y0, x1, y1]})
    cont_blocks = continued_blocks(lines_by_page, caps, heights)
    caps = caps + continued_captions(doc, lines_by_page, body, caps)
    others = caps + cont_blocks + bodies
    out, rot_blocks = rotated_figures(doc, rot_caps, body, typ_w, bands, others)
    others = others + rot_blocks
    for cap in caps:
        pno = cap["page"]
        page = doc[pno - 1]
        lines = lines_by_page[pno - 1]
        paras = paragraph_lines(lines, body, page.rect.height, typ_w)
        # 表は下が基本、それ以外は上が基本。だめなら反対側へ。
        order = ["below", "above"] if cap["kind"] == "table" else ["above", "below"]
        # 段の幅の 7 割より細い複数行のキャプションは、横置き（右に図）を先に試す（Springer）
        # （Wiley の横置きキャプションは 172pt＝典型の行幅の 7 割を少し超えるものがあり、8 割5分まで広げた）
        # 回転の付いたページ（Fergus 1969 の図版は 270°）は座標と見た目の左右が食い違うので横は探さない
        if (cap["kind"] != "table" and len(cap["chain"]) >= 2 and page.rotation == 0
                and cap["bbox"][2] - cap["bbox"][0] < 0.85 * typ_w):
            # キャプションがページの右寄りなら左に図（Wiley）、左寄りなら右に図（Springer）
            order = (["left"] if cap["bbox"][0] > page.rect.width / 2 else ["right"]) + order
        chosen = None
        chosen_where = order[0]
        for where in order:
            clip, region = _make_clip(cap, page, paras, others, where, bands)
            if clip is not None and cap["kind"] != "table" and cap["layout"] != "full" and where in ("above", "below"):
                clip, region, cap["layout"] = widen_cut(cap, page, paras, others, clip, region, where, bands)
            if (where == "below" and cap["kind"] != "table" and clip is not None
                    and any(o["page"] == pno and o.get("kind") in ("figure", "scheme") and o["id"] != cap["id"]
                            and abs(o["bbox"][1] - region[3]) < 1 for o in others)):
                # 下の領域が別の図のキャプションの真上で終わる＝その墨は下の図のもの（図はキャプションの上が基本）。
                # Nomura 2025 Fig.4 が上の自分の図を捨て、下の Fig.5 の絵を取った
                continue
            if clip is not None and _big_enough(clip):
                chosen, chosen_where = clip, where
                break
            if chosen is None and clip is not None:
                chosen, chosen_where = clip, where   # 小さいが空よりまし（low になる）
        conf = _confidence(chosen, paras)
        natural = "below" if cap["kind"] == "table" else "above"
        if chosen is not None and chosen_where not in (natural, "right", "left"):
            conf = "low"          # 想定と逆側から取ったものは必ず人の目で確認する
        tb = _table_bbox(page, cap) if cap["kind"] == "table" else None
        if tb is not None:        # 表は罫線と文字から取った外枠を優先する
            chosen, chosen_where, conf = tb, "table", "high"
        if chosen is None:
            # 何も取れなければキャプション直上の細い帯を置いておく（override 前提・low）
            cx0, cx1 = _col_range(cap["layout"], page.rect.width)
            chosen = (cx0, max(cap["bbox"][1] - 100, bands[0]), cx1, cap["bbox"][1] - 1)
            conf = "low"
        out.append({
            "id": cap["id"], "number": cap["number"], "label": cap["label"], "kind": cap["kind"],
            "page": pno, "caption": cap.get("caption") or _join_caption(cap["chain"]),
            **({"continues": cap["continues"]} if cap.get("continues") else {}),
            "caption_bbox": [round(v, 1) for v in cap["bbox"]],
            "clip": [round(v, 1) for v in chosen],
            "layout": cap["layout"], "confidence": conf, "source": "auto",
            "found": chosen_where,
        })
    # ページ順・種類(figure→scheme→table)・番号順
    def key(f):
        num = re.sub(r"\D", "", f["number"]) or "0"
        return ({"figure": 0, "scheme": 1, "table": 2}[f["kind"]], int(num), f["page"])
    out.sort(key=key)
    return out


def load_figures(work_dir) -> list[dict]:
    """work/<id>/figures.json を読み、figures_override.json があれば id で上書きして返す。
    override は部分指定でもよい（例: {"id":"fig3","clip":[...]}）。auto に無い id は新規追加。"""
    work = Path(work_dir)
    figs = json.loads((work / "figures.json").read_text(encoding="utf-8"))
    ov_path = work / "figures_override.json"
    if not ov_path.exists():
        return figs
    overrides = json.loads(ov_path.read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in figs}
    order = [f["id"] for f in figs]
    for o in overrides:
        merged = dict(by_id.get(o["id"], {}))
        merged.update(o)
        merged["source"] = "override"
        merged.setdefault("confidence", "high")
        if o["id"] not in by_id:
            order.append(o["id"])
        by_id[o["id"]] = merged
    return [by_id[i] for i in order]


def render_figure(doc, fig: dict, out_png, long_edge: int = 1800) -> float:
    """fig["clip"] を長辺 long_edge px の PNG に描画し、倍率（px/pt）を返す。
    geometry.py など後段は pt→px をこの倍率で換算する。向きの違う図表は正立させて描く（orient.figure_view）。"""
    page, clip = figure_view(doc, fig)
    scale = long_edge / max(clip.width, clip.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False, annots=False)  # 読者の注釈は描かない
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_png))
    return scale
