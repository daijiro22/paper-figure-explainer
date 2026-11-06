"""PDF のテキスト行を扱う共通ヘルパ（figures.py / text.py / cite.py が使う）。

座標はすべて pt（PDF ポイント、ページ左上原点・y は下向き）。
このモジュールは「行」を単位にする。PyMuPDF のブロックは本文と図のキャプションが
同じブロックに混ざることがある（例: Fig. 2 のキャプションは右段の本文と同じブロック）ので、
ブロックでなく行（line）と、行ごとの支配的フォントサイズで本文/キャプション/柱を見分ける。
"""
from __future__ import annotations

import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 文字化けしやすい記号の正規化。NFKC を丸ごとかけると上付き数字（²→2）が壊れるので明示的に置換する。
_CHAR_MAP = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "◦": "°",      # ◦ （度の小さい丸。本文では ◦C と別 span で出る）
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "﻿": "",
}
_CHAR_TABLE = {ord(k): v for k, v in _CHAR_MAP.items()}
# 制御文字（タブ・改行以外の C0）は空白にする。Springer の見出しは "2.2.4 \x07Pyrolysis" で、画面に □ が出た
_CHAR_TABLE.update({c: " " for c in range(32) if chr(c) not in "\t\n\r"})


def norm_chars(s: str) -> str:
    """ligature・空白類・度記号の軽い正規化（ソフトハイフンはここでは触らない）。"""
    return s.translate(_CHAR_TABLE)


# 図表キャプションの先頭。"Fig. 2." "Figure 3:" "Scheme 1." "Table 1." など。
# 図表キャプションの書き出し。番号の後ろは「. / : / |」（Elsevier・ACS）か、空白のあとに本文（Springer・RSC は
# "Fig. 1  Lignin ..." と句点が無い）か、行末。空白のあとが動詞なら本文中の言及（"Fig. 5 shows ..."）として除く。
# 前置詞は除外語に入れない（"Fig. 2 In situ ..." というキャプションがありうる）。言及とキャプションは主に
# 「本文より小さい字」で分ける（figures._find_captions）。"Fig. 4a" のように番号に字が続くものは言及。
_MENTION_WORDS = (r"shows?|is|are|was|were|presents?|illustrates?|depicts?|displays?|gives?|compares?|summari[sz]es?|"
                  r"indicates?|demonstrates?|reveals?|suggests?")
# 番号はアラビア数字のほか、大文字のローマ数字（JJAP の "Table I."）も受ける。小文字は本文の語（"Table in"）と紛れるので受けない
CAPTION_RE = re.compile(r"^\s*(Fig\.|Figure|Scheme|Table)\s*(S?\d+|(?-i:[IVX]+))"
                        r"(?:\s*[.:|)]|\s+(?!(?:" + _MENTION_WORDS + r")\b)(?=\S)|\s*$)", re.I)   # ")" は RSC の原稿（"Figure 2) …"）


BODY_TIE = 0.9      # より大きい字がこの割合以上あれば本文とみなす（body_font_size）


class Line(dict):
    """行 1 本。キーは page(1始まり) bbox(x0,y0,x1,y1) text size bold italic spans。
    italic は Elsevier の 2.1. 3.2. 等の小見出し判定に使う。"""

    @property
    def x0(self): return self["bbox"][0]
    @property
    def y0(self): return self["bbox"][1]
    @property
    def x1(self): return self["bbox"][2]
    @property
    def y1(self): return self["bbox"][3]


def _build_line(pno: int, bbox, spans) -> Line:
    """span 列から Line を作る（支配的サイズ・太字・斜体を計算）。"""
    text = "".join(s["text"] for s in spans)
    cnt: Counter = Counter()
    for s in spans:
        n = len(s["text"].strip())
        if n:
            cnt[round(s["size"], 1)] += n
    size = cnt.most_common(1)[0][0] if cnt else 0.0
    vis = [s for s in spans if s["text"].strip()]
    bold = bool(vis) and all((s["flags"] & 16) or ("Bold" in s["font"]) for s in vis)
    italic = bool(vis) and all((s["flags"] & 2) or ("Italic" in s["font"]) for s in vis)
    return Line(page=pno, bbox=tuple(bbox), text=norm_chars(text), size=size,
                bold=bold, italic=italic, spans=spans)


def _merge_same_row(lines: list[Line]) -> list[Line]:
    """PyMuPDF が 1 行を単語ごとの「行」に割ってしまうことがある（Taira 論文 p9 の
    "are formed preferentially from polysaccharides at elevated temperatures."）。
    同じ高さ・隣り合う（間隔 10pt 未満）行は 1 行にまとめる。段の間の隙間(16pt)は越えない。
    左から順に見て、すぐ左に終わる同じ高さの行を探してつなぐ（RSC の図では "Fig. 2" の札と
    その右のキャプション本文が別の行で、y0 が 670.5 と 670.2。round(y0) で並べると別の組に落ちてつながらなかった）。"""
    out: list[Line] = []
    for l in sorted(lines, key=lambda l: (l.x0, l.y0)):
        hit = None
        for i, p in enumerate(out):
            same_row = abs(l.y0 - p.y0) < 2 and abs(l.y1 - p.y1) < 2
            if same_row and -1 <= l.x0 - p.x1 < 10:
                hit = i
                break
        if hit is None:
            out.append(l)
            continue
        p = out[hit]
        spans = list(p["spans"])
        # 単語間の空白が失われるので、境界に空白 span が無ければ 1 つ足す
        if not (p["text"].endswith(" ") or l["text"].startswith(" ")):
            spans = spans + [dict(spans[-1], text=" ")]
        spans = spans + list(l["spans"])
        out[hit] = _build_line(p["page"], (p.x0, min(p.y0, l.y0), l.x1, max(p.y1, l.y1)), spans)
    return sorted(out, key=lambda l: (round(l.y0), l.x0))


def page_lines(page, pno: int | None = None) -> list[Line]:
    """ページの全テキスト行（空行は除く）。size は文字数で重み付けした支配的フォントサイズ。"""
    out: list[Line] = []
    pno = pno if pno is not None else page.number + 1
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"]]
            if not "".join(s["text"] for s in spans).strip():
                continue
            out.append(_build_line(pno, l["bbox"], spans))
    return _merge_same_row(out)


def all_lines(doc) -> list[list[Line]]:
    """ページごとの行リスト（インデックス 0 = 1 ページ目）。"""
    return [page_lines(p, i + 1) for i, p in enumerate(doc)]


def body_font_size(doc) -> float:
    """文書全体で最も多い span サイズ（文字数重み）= 本文サイズ。ハードコードしない。
    より大きい字がほぼ同じ量あれば、大きい方を本文とする（Luo 2011: 実験方法と文献の 6.4pt が本文の 8pt を
    わずかに上回り、キャプション 6.4pt が「本文より小さい行」にならず図が 0 枚になった）。"""
    cnt: Counter = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    n = len(s["text"].strip())
                    if n:
                        cnt[round(s["size"], 1)] += n
    top, n_top = cnt.most_common(1)[0]
    return max(s for s, n in cnt.items() if s == top or (s > top and n >= BODY_TIE * n_top))


def is_body(line: Line, body: float, tol: float = 0.3) -> bool:
    return abs(line["size"] - body) <= tol


def is_small(line: Line, body: float, tol: float = 0.3) -> bool:
    """本文より小さい行（キャプション・柱・表・参考文献など）。"""
    return line["size"] < body - tol


def in_margin(line: Line, page_height: float) -> bool:
    """柱（ヘッダー）・ページ番号などの余白帯。Elsevier（792pt）では y<48 と y>750 に柱とページ番号がある。
    ページの高さに比例させる（RSC は 779.5pt・Springer は 790.9pt）。高さ不明（0）なら 792pt とみなす。"""
    head, foot = margin_bands(page_height)
    return line.y1 < head or line.y0 > foot


def margin_bands(page_height: float) -> tuple[float, float]:
    """柱の帯の下端と、ページ番号の帯の上端（pt）。Elsevier の 48/750（792pt）を比で他の判型へ写す。"""
    ph = page_height or 792.0
    return ph * 48 / 792, ph * 750 / 792


def column_of(x0: float, x1: float, page_width: float) -> str:
    """矩形の x 範囲から段を判定。left / right / full。"""
    mid = page_width / 2
    if x1 <= mid + 3:
        return "left"
    if x0 >= mid - 3:
        return "right"
    return "full"


def rect_intersect(a, b):
    """矩形 a, b の共通部分 (x0,y0,x1,y1)。無ければ None。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 < x0 or y1 < y0:
        return None
    return (x0, y0, x1, y1)


def overlap_size(a, b) -> tuple[float, float]:
    """矩形 a, b の重なりの (幅, 高さ)。重ならなければ (0, 0)。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return (max(w, 0.0), max(h, 0.0))
