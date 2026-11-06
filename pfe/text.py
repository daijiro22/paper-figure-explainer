"""本文の抽出（text.json を作る）。

流れ:
1. ページごとに行を取り、本文サイズ(最多の span サイズ)の行だけを残す。
   キャプション・柱・ページ番号・表・参考文献は本文サイズより小さいので自然に落ちる。
   念のためキャプション矩形に入る行・「References」見出し以降も落とす。
2. 段組を読み順に並べる。全幅の図（キャプション含む）をはさんで上の帯→下の帯の順、帯の中は左段→右段。
3. 行を段落にまとめる。段落の境目 = 字下げ行・見出し行・直前の行が短い（段落末）。
   段・ページをまたぐ段落は連結する（図キャプションが間に挟まっていても行から除いてあるので連続する）。
4. 文に分割し、各文から図の言及(figs)と数字引用(cites)を拾う。
"""
from __future__ import annotations

import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from collections import Counter

from .pdfutil import (BODY_TIE, Line, all_lines, body_font_size, column_of, in_margin,
                      is_body, overlap_size)

# ---------------------------------------------------------------------------
# 文分割
# ---------------------------------------------------------------------------

# ピリオドの直前の語がこれなら文末ではない（小文字で比較）。
_ABBREV = {
    "fig", "figs", "figure", "figures", "et al", "al", "e.g", "i.e", "ca", "approx", "vs", "no", "nos",
    "eq", "eqs", "ref", "refs", "cf", "resp", "wt", "vol", "ed", "eds", "pp", "p", "dr", "mr", "mrs", "prof",
    "st", "sec", "ch", "temp", "conc", "ave", "avg", "max", "min", "sp", "spp", "var", "viz", "c", "ie", "eg",
    "inc", "co", "ltd", "corp", "jr", "sr", "fig. s",
}
# 文頭になりうる文字（大文字・数字・開き括弧・引用符・ギリシャ文字大文字）
_SENT_START = r"[A-Z0-9\[\(“\"‘'Α-Ω]"
_CAND_RE = re.compile(r"([.!?])([\)\]”\"’']*)(\s+)(?=" + _SENT_START + ")")


def split_sentences(text: str) -> list[str]:
    """略語・小数・イニシャルで切らない文分割。"""
    out: list[str] = []
    start = 0
    for m in _CAND_RE.finditer(text):
        end = m.end(2)                       # ピリオド（と閉じ括弧）まで含める
        if m.group(1) == ".":
            head = text[start:m.start(1)]
            tok = re.search(r"(\S+)$", head)
            word = tok.group(1) if tok else ""
            low = word.lower().strip("([{“\"'")
            # 略語（Fig. / et al. / e.g. / vs. / No. …）
            if low in _ABBREV or re.search(r"\bet al$", low) or low.endswith(("e.g", "i.e")):
                continue
            # 1 文字のイニシャル（J. / E.）や J.E. のように 1 文字+ピリオドの連続
            if re.fullmatch(r"(?:[A-Za-z]\.)*[A-Za-z]", word.strip("([")):
                # ただし "A." 等で文が終わる場合（"...vitamin C."）は稀なので切らない側に倒す
                continue
        seg = text[start:end].strip()
        if seg:
            out.append(seg)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


# ---------------------------------------------------------------------------
# 図の言及・引用の抽出
# ---------------------------------------------------------------------------

_DASH = "–—−‐-"
# Fig. 1 / Figs. 2 and 3 / Figure 2–4 / Fig. S2 / Scheme 1 / Table 1
_MENTION_HEAD = re.compile(r"\b(Figs?\.?|Figures?|Schemes?|Tables?|Tabs?\.)\s*(?=S?\d)", re.I)
# 古い論文のローマ数字（Table III）。OCR で I が 1 や l に化ける（Table I11 → III）。大文字で始まる語だけ
_ROMAN_HEAD = re.compile(r"\b(Figs?\.?|Figures?|Tables?|Tabs?\.|TABLES?)\s+([IVX][IVX1l]*)\b"
                         r"((?:\s*(?:,|and|&)\s*[IVX][IVX1l]*\b)*)")
_ROMAN_VALUE = {"I": 1, "V": 5, "X": 10}
# OCR で 1 が l に化ける（Figure l）
_OCR_ONE = re.compile(r"\b(Figs?\.?|Figures?|Tables?)\s+l\b")
_ITEM = re.compile(r"(S?)(\d+)(?:\s?\(?[a-z]\)?(?![a-z]))?")
_SEP_LIST = re.compile(r"\s*(?:,|;|and|&|or)\s*(?=S?\d)")
_SEP_RANGE = re.compile(r"\s*(?:[" + _DASH + r"]|to)\s*(?=S?\d)")


def _mention_prefix(word: str) -> str:
    w = word.lower().rstrip(".")
    if w.startswith("fig"):
        return "fig"
    if w.startswith("scheme"):
        return "scheme"
    return "table"


def _roman(token: str) -> int | None:
    """III → 3、I11 → 3（OCR の化けを戻す）。ローマ数字として読めなければ None。"""
    s = token.replace("1", "I").replace("l", "I")
    if not re.fullmatch(r"X{0,3}(IX|IV|V?I{0,3})", s) or not s:
        return None
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN_VALUE[a]
        total += -v if b in _ROMAN_VALUE and _ROMAN_VALUE[b] > v else v
    return total


def find_fig_mentions(sentence: str) -> list[str]:
    """文中の図表言及を id のリストにする（出現順・重複なし・範囲は展開）。
    Fig. 5a → fig5 / Fig. S2 → figS2 / Figures 2–4 → fig2, fig3, fig4 / Tables II and III → table2, table3。"""
    ids: list[str] = []

    def add(i: str):
        if i not in ids:
            ids.append(i)

    sentence = _OCR_ONE.sub(lambda m: f"{m.group(1)} 1", sentence)
    for m in _ROMAN_HEAD.finditer(sentence):
        prefix = _mention_prefix(m.group(1))
        for tok in [m.group(2)] + re.findall(r"[IVX][IVX1l]*", m.group(3) or ""):
            n = _roman(tok)
            if n:
                add(f"{prefix}{n}")
    for m in _MENTION_HEAD.finditer(sentence):
        prefix = _mention_prefix(m.group(1))
        pos = m.end()
        prev_num = None
        prev_s = ""
        pending_range = False
        while True:
            im = _ITEM.match(sentence, pos)
            if not im:
                break
            s, n = im.group(1), int(im.group(2))
            if pending_range and prev_num is not None and n > prev_num and n - prev_num < 30:
                for k in range(prev_num + 1, n + 1):
                    add(f"{prefix}{s}{k}")
            else:
                add(f"{prefix}{s}{n}")
            prev_num, prev_s = n, s
            pos = im.end()
            rm = _SEP_RANGE.match(sentence, pos)
            if rm:
                pending_range = True
                pos = rm.end()
                continue
            lm = _SEP_LIST.match(sentence, pos)
            if lm:
                pending_range = False
                pos = lm.end()
                continue
            break
    return ids


_CITE_RE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+|\s*[" + _DASH + r"]\s*\d+)*)\]")


def find_cites(sentence: str) -> list[int]:
    """[12] [3–5] [2,6] [7,9–11] を番号リストに展開（昇順・重複なし）。"""
    nums: set[int] = set()
    for m in _CITE_RE.finditer(sentence):
        body = m.group(1)
        # カンマで分け、各要素が範囲なら展開
        for part in re.split(r"\s*[,;]\s*", body):
            rm = re.fullmatch(r"(\d+)\s*[" + _DASH + r"]\s*(\d+)", part.strip())
            if rm:
                lo, hi = int(rm.group(1)), int(rm.group(2))
                if lo <= hi < lo + 100:
                    nums.update(range(lo, hi + 1))
                else:
                    nums.update((lo, hi))
            elif part.strip().isdigit():
                nums.add(int(part.strip()))
    return sorted(nums)


# ---------------------------------------------------------------------------
# 行 → 段落
# ---------------------------------------------------------------------------

def _slug(title: str) -> str:
    """見出しから section id を作る。"3.1. TG-MS analysis" → s3_1、番号が無ければ s_<英数字>。"""
    m = re.match(r"^\s*(?:Appendix\s+)?([A-Z]?\d+(?:\.\d+)*)\.?\s", title + " ")
    if m:
        return "s" + m.group(1).replace(".", "_")
    base = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()
    return "s_" + (base[:30] or "x")


# 参考文献の見出し（本文の字の大きさを数える範囲を決める）
_REF_HEAD = re.compile(r"^\s*(?:\d+\.?\s*)?(?:references|bibliography|literature cited|notes and references)\s*:?\s*$", re.I)
BODY_SHARE = 0.15   # 本文の前半でこの割合以上を占める別の大きさも本文とみなす（実験の節だけ小さい組版）
BODY_NEAR = 2.0     # 本文とみなす大きさの差（pt）。これより違う字は表題か注
BODY_PAGE_CHARS = 600  # このページの最多の大きさを本文とみなす最低の字数（走査 PDF はページごとに大きさが違う）


def _body_test(doc, lines_by_page, figures):
    """本文の行かを判定する関数と、主な本文の大きさを返す。

    字の大きさを 1 つに決めると 3 つの型で本文を失った（2026-11-06）:
    - 参考文献が本文より多い: 参考文献の大きさを本文と取り違え、本文が全部消えた（Bakulin 2024）
    - 走査 PDF の OCR: ページごとに大きさが違い、半分のページが消えた（Jakab 1997）
    - 実験の節だけ小さい組版: その節が消えた（Chang 1975）
    主な大きさの行は今までどおり本文とする。ほかの大きさは参考文献より前で、図表の枠の外の行だけを足す。
    """
    # 参考文献の見出しの位置（後ろ半分の最後のもの）。ここから後は数えない
    n_pages = len(lines_by_page)
    cut = None
    for pno in range(n_pages, n_pages // 2, -1):
        hits = [l for l in lines_by_page[pno - 1] if _REF_HEAD.match(l["text"])]
        if hits:
            cut = (pno, hits[-1].y0)
            break

    def before_refs(l) -> bool:
        return cut is None or l["page"] < cut[0] or (l["page"] == cut[0] and l.y0 < cut[1])

    total: Counter = Counter()
    by_page: dict[int, Counter] = {}
    for lines in lines_by_page:
        for l in lines:
            n = len(l["text"].strip())
            if n and before_refs(l) and not in_margin(l, doc[l["page"] - 1].rect.height):
                total[l["size"]] += n
                by_page.setdefault(l["page"], Counter())[l["size"]] += n
    if not total:
        main = body_font_size(doc)
        return (lambda l: is_body(l, main)), main
    top, n_top = total.most_common(1)[0]
    main = max(s for s, n in total.items() if s == top or (s > top and n >= BODY_TIE * n_top))
    all_chars = sum(total.values())
    extra = {s for s, n in total.items() if n >= BODY_SHARE * all_chars and abs(s - main) <= BODY_NEAR}
    page_extra = {}
    for pno, c in by_page.items():
        if sum(c.values()) >= BODY_PAGE_CHARS:
            s = c.most_common(1)[0][0]
            if abs(s - main) <= BODY_NEAR:
                page_extra[pno] = s
    clips = {}
    for f in figures:
        if f.get("clip"):
            clips.setdefault(f["page"], []).append(f["clip"])

    def test(l) -> bool:
        if is_body(l, main):
            return True
        if not before_refs(l):
            return False
        sizes = set(extra)
        if l["page"] in page_extra:
            sizes.add(page_extra[l["page"]])
        if not any(is_body(l, s) for s in sizes):
            return False
        # 図の中の文字・表のセルを本文にしない
        cx, cy = (l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2
        return not any(c[0] <= cx <= c[2] and c[1] <= cy <= c[3] for c in clips.get(l["page"], []))

    return test, main


def _reading_order(doc, lines_by_page, figures, is_b):
    """本文行を読み順に並べた (page, Line) のリスト。
    全幅の図/キャプションを区切りにして、帯ごとに 左段 → 右段。"""
    ordered: list[Line] = []
    for pno, lines in enumerate(lines_by_page, 1):
        page = doc[pno - 1]
        pw, ph = page.rect.width, page.rect.height
        caps = [f for f in figures if f["page"] == pno]
        # 全幅の図は clip と caption_bbox を合わせた縦範囲を帯の区切りにする
        seps = []
        for f in caps:
            if f.get("layout") == "full":
                seps.append((min(f["clip"][1], f["caption_bbox"][1]), max(f["clip"][3], f["caption_bbox"][3])))
        seps.sort()

        def band_of(y: float) -> int:
            b = 0
            for s0, s1 in seps:
                if y >= s1 - 1:
                    b += 1
            return b

        cand = []
        for l in lines:
            if not is_b(l) or in_margin(l, ph):
                continue
            # キャプション矩形に入る行は本文でない（念のため）
            cx, cy = (l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2
            if any(f["caption_bbox"][0] - 1 <= cx <= f["caption_bbox"][2] + 1 and
                   f["caption_bbox"][1] - 1 <= cy <= f["caption_bbox"][3] + 1 for f in caps):
                continue
            col = column_of(l.x0, l.x1, pw)
            cand.append((band_of(l.y0), {"left": 0, "right": 1, "full": 0}[col], l.y0, l))
        cand.sort(key=lambda t: (t[0], t[1], t[2]))
        ordered.extend(t[3] for t in cand)
    return ordered


def _join_lines(parts: list[str]) -> str:
    """行を連結する。行末のソフトハイフン/ハイフンは語を戻す（soft­ wood → softwood）。"""
    s = ""
    for t in parts:
        t = t.strip()
        if not t:
            continue
        if s.endswith("­"):
            s = s[:-1] + t
        elif re.search(r"[A-Za-z]-$", s) and re.match(r"[a-z]", t):
            s = s + t           # "TG-" + "MS" は大文字始まりなので別扱い（下の else）、小文字始まりは語の継続とみなす
        elif s.endswith("-"):
            s = s + t           # "TG-" "MS"、"β-" "ether" など。ハイフンは残して詰める
        else:
            s = (s + " " + t) if s else t
    # 行内に残ったソフトハイフン（+空白）も戻す
    s = re.sub("­\\s*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_text(doc, figures: list[dict]) -> dict:
    """本文を段落・文に分けて text.json の dict を返す。"""
    lines_by_page = all_lines(doc)
    is_b, _main = _body_test(doc, lines_by_page, figures)
    ordered = _reading_order(doc, lines_by_page, figures, is_b)

    # 1 ページ目は表題・著者・抄録の前置きがあるので、最初の見出し（"1. Introduction"）より前は捨てる
    first_heading_y = None
    for l in lines_by_page[0]:
        if is_b(l) and l["bold"] and re.match(r"^\s*1\.\s+\w", l["text"]):
            first_heading_y = l.y0
            break
    cleaned: list[Line] = []
    for l in ordered:
        if l["page"] == 1 and first_heading_y is not None and l.y0 < first_heading_y - 2:
            continue
        cleaned.append(l)

    # 段の左端・右端（字下げ・短い行の判定に使う）。ページの左右段ごとに本文行の最頻 x0 / 最大 x1 を取る。
    def col_edges(pno: int, col: str):
        pw = doc[pno - 1].rect.width
        xs0 = [round(l.x0) for l in lines_by_page[pno - 1] if is_b(l) and column_of(l.x0, l.x1, pw) == col]
        xs1 = [round(l.x1) for l in lines_by_page[pno - 1] if is_b(l) and column_of(l.x0, l.x1, pw) == col]
        if not xs0:
            return None
        from collections import Counter
        return (Counter(xs0).most_common(1)[0][0], max(xs1))

    paragraphs: list[dict] = []
    sections: list[dict] = []
    cur_section = None
    cur: dict | None = None
    prev: Line | None = None
    heading_buf: list[Line] = []
    stop = False

    def flush_heading():
        nonlocal cur_section, cur, heading_buf, stop
        if not heading_buf:
            return
        title = _join_lines([h["text"] for h in heading_buf])
        heading_buf = []
        if title.lower().startswith("references"):
            stop = True            # 参考文献リスト以降は段落にしない
            return
        sid = _slug(title)
        if all(s["id"] != sid for s in sections):
            sections.append({"id": sid, "title": title})
        cur_section = sid
        cur = None

    def start_para(l: Line):
        nonlocal cur
        cur = {"section": cur_section, "page": l["page"], "parts": []}
        paragraphs.append(cur)

    # 段の本文左端（最頻 x0）と右端をキャッシュ
    edge_cache: dict = {}

    def edges(l: Line):
        pw = doc[l["page"] - 1].rect.width
        col = column_of(l.x0, l.x1, pw)
        key = (l["page"], col)
        if key not in edge_cache:
            edge_cache[key] = col_edges(l["page"], col)
        return edge_cache[key]

    def is_heading_line(l: Line) -> bool:
        """太字行 = 見出し。斜体の「3.2. …」は小見出し（その 2 行目の斜体行も見出しの続き）。"""
        if l["bold"]:
            return True
        if l["italic"]:
            if re.match(r"^\s*\d+\.\d+\.?\s", l["text"]):
                return True
            return bool(heading_buf) and prev is not None and prev in heading_buf and l.y0 - prev.y1 < 4
        return False

    for l in cleaned:
        if stop:
            break
        if is_heading_line(l):
            # 太字行 = 見出し。続く太字行（2 行見出し）は連結する
            if heading_buf and prev is not None and prev in heading_buf and l.y0 - prev.y1 < 4 and l["page"] == prev["page"]:
                heading_buf.append(l)
            else:
                flush_heading()
                if stop:
                    break
                heading_buf = [l]
            prev = l
            continue
        # 本文行: 保留中の見出しを確定
        flush_heading()
        if stop:
            break
        e = edges(l)
        new_para = cur is None
        if not new_para and e is not None:
            indented = l.x0 - e[0] > 4
            # 直前の行が（その行の段の右端より）25pt 以上短ければ段落末 → 次は新段落
            pe = edges(prev) if prev is not None else None
            short_prev = pe is not None and pe[1] - prev.x1 > 25
            if indented or short_prev:
                new_para = True
        if new_para:
            start_para(l)
        cur["parts"].append(l["text"])
        prev = l
    flush_heading()

    # 段落テキスト化・文分割・言及/引用抽出
    out_pars: list[dict] = []
    fig_mentions: dict[str, list[dict]] = {}
    n = 0
    for p in paragraphs:
        text = _join_lines(p["parts"])
        if not text:
            continue
        n += 1
        pid = f"p{n}"
        sents = []
        for i, s in enumerate(split_sentences(text)):
            figs = find_fig_mentions(s)
            sents.append({"i": i, "text": s, "figs": figs, "cites": find_cites(s)})
            for fid in figs:
                fig_mentions.setdefault(fid, []).append({"para": pid, "sentence": i})
        out_pars.append({"id": pid, "section": p["section"], "page": p["page"],
                         "text": text, "sentences": sents})
    return {"sections": sections, "paragraphs": out_pars, "fig_mentions": fig_mentions}
