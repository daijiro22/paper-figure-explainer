"""段やページをまたぐ図: 短い題で片方の段と判定された段抜きの図（widen_cut）と、続きのページの図（continued_*）。

続きのページ（"Fig. 7. (continued)."）について:
- 続きの行は 2 通りに使う: 図の探索の境界（continued_blocks）と、図の続きの切り出し（continued_captions）。
- 図の続きは 1 枚につながず、別の項目 <親の id>-cont にする（札は「Fig. 7（続き）」）。パネルが 1 ページずつ埋まっている図を
  つなぐと縦に 2 ページ分の画像になり、画面で字が読めない（Wang 2025 Fig.7 の Softwood / Hardwood、Wang 2026 Fig.3）。
- 続きの題は親の題の繰り返しなので、caption は親の本文＋ " (continued)"、本文の言及と引用は親のものを使う（項目の continues に親の id）。
- 表の続きと、向きの違うページの図の続きはまだ取らない（表は境界にするだけ）。
"""
from __future__ import annotations

import re

import pymupdf

from .pdfutil import CAPTION_RE, column_of, in_margin, is_small, overlap_size

CONT_MARK_RE = re.compile(r"\bcont(?:inued|'?d|\.)", re.I)
# 題の番号のすぐ後ろに続きの印だけがある行（"Fig. 7. (continued)."）。本文の "Fig. 5. Continued heating of ..." は別物だが、
# ふつうの題としては今も捨てているので（_find_captions）ここでは字の小ささと親の有無で絞る
_CONT_RIGHT_AFTER_RE = re.compile(r"^\W*cont(?:inued|'?d|\.)", re.I)


def continued_blocks(lines_by_page, caps: list[dict], doc_heights: list[float]) -> list[dict]:
    """続きのキャプション行（"Table 2. continued" など）と、その下に行間 10pt 未満で続く行の塊。

    キャプションとしては数えないが、図の探索では境界にしないと表が図に入る
    （Jia 2017 ACS の Fig. 3 が、同じページ上部の Table 2 の続きを丸ごと取り込んだ）。"""
    from . import figures as F
    first = {(c["id"], c["page"], round(c["bbox"][1])) for c in caps}
    out = []
    for pno, lines in enumerate(lines_by_page, 1):
        for ln in lines:
            # 続きの印（continued / cont'd）がある行だけ。字の大きさは問わない（ACS の表題は本文と同じ 10pt）
            m = CAPTION_RE.match(ln["text"])
            if not m or in_margin(ln, doc_heights[pno - 1]) or not CONT_MARK_RE.search(ln["text"]):
                continue
            fid = F._fig_id(m.group(1), F._arabic(m.group(2)))
            if (fid, pno, round(ln.y0)) in first:
                continue
            x0, y0, x1, y1 = ln.x0, ln.y0, ln.x1, ln.y1
            grown = True
            while grown:
                grown = False
                for c in lines:
                    # 表題と表の見出し行の間は 9pt ほど空く（Jia 2017 ACS）。10pt 未満の行間なら同じ表とみなす
                    if y1 - 1 < c.y0 < y1 + 10 and c.y1 > y1 and c.x1 > x0 and c.x0 < x1:
                        x0, x1, y1 = min(x0, c.x0), max(x1, c.x1), max(y1, c.y1)
                        grown = True
            out.append({"id": f"{fid}-cont", "page": pno, "bbox": [x0, y0, x1, y1]})
    return out


def continued_captions(doc, lines_by_page, body: float, caps: list[dict]) -> list[dict]:
    """図（Fig.・Scheme）の続きの行を、ふつうのキャプションと同じ形で返す。前のページに親のキャプションがあるものだけ。"""
    from . import figures as F
    parents = {c["id"]: c for c in caps if c["kind"] != "table"}
    seen: dict[str, int] = {}
    out = []
    for pno, lines in enumerate(lines_by_page, 1):
        page = doc[pno - 1]
        for ln in lines:
            m = CAPTION_RE.match(ln["text"])
            if not m or not _CONT_RIGHT_AFTER_RE.match(ln["text"][m.end():]):
                continue
            if not is_small(ln, body) or in_margin(ln, page.rect.height):
                continue
            parent = parents.get(F._fig_id(m.group(1), F._arabic(m.group(2))))
            if parent is None or parent["page"] >= pno:
                continue
            n = seen[parent["id"]] = seen.get(parent["id"], 0) + 1
            out.append({
                "id": f"{parent['id']}-cont" + (str(n) if n > 1 else ""),
                "number": parent["number"], "label": f"{parent['label']}（続き）", "kind": parent["kind"],
                "page": pno, "chain": [ln], "bbox": [ln.x0, ln.y0, ln.x1, ln.y1],
                "layout": column_of(ln.x0, ln.x1, page.rect.width),
                "continues": parent["id"], "caption": F._join_caption(parent["chain"]) + " (continued)",
            })
    return out


def widen_cut(cap, page, paras, others, clip, region, where, bands):
    """段に収めた枠の内側の端で墨が切れていれば段抜きの図とみなし、全幅で取り直す。(clip, region, layout) を返す。
    段の判定はキャプションの幅だけなので、1 行の短い題だと段抜きの図を左半分で切った（Yao 2024 Fig.1: 題の幅 140pt・墨は x=42〜553）。
    隣の段に別の図表が同じ高さで並ぶとき（左右に 2 枚の図）と、全幅にすると隣の段の本文で図の高さが縮むときは取り直さない。"""
    from . import figures as F
    keep = clip, region, cap["layout"]
    strip = ((region[2] - 2, region[1], region[2], region[3]) if cap["layout"] == "left"
             else (region[0], region[1], region[0] + 2, region[3]))
    if F._ink_bbox(page, strip) is None:
        return keep
    top, bottom = min(clip[1], cap["bbox"][1]), max(clip[3], cap["bbox"][3])
    for o in others:
        if (o["page"] == cap["page"] and o["id"] != cap["id"] and o["bbox"][1] < bottom and o["bbox"][3] > top
                and overlap_size(o["bbox"], (0, top, page.rect.width, bottom))[0] > 5):
            return keep
    wide, wide_region = F._make_clip(dict(cap, layout="full"), page, paras, others, where, bands)
    if wide is None or wide[3] - wide[1] < 0.8 * (clip[3] - clip[1]):
        return keep
    # 隣の段の下に別の図の題があり、この図の題の高さでも隣の段に墨が続くなら、隣の段の墨はその図のもの
    # （Fukutome 2016 Fig.5 が左の段の Fig.4 を抱き込んだ。墨が切れていれば下の図は別物: Zulkifli 2023 Fig.9）
    opposite = "right" if cap["layout"] == "left" else "left"
    opp_band = ((clip[2], cap["bbox"][1], wide[2], cap["bbox"][3]) if opposite == "right"
                else (wide[0], cap["bbox"][1], clip[0], cap["bbox"][3]))
    for o in others:
        if (o["page"] == cap["page"] and o["id"] != cap["id"] and o.get("kind") in ("figure", "scheme")
                and column_of(o["bbox"][0], o["bbox"][2], page.rect.width) == opposite and o["bbox"][1] > wide[1]
                and F._ink_bbox(page, opp_band) is not None):
            return keep
    # 隣の段に長い行が続くなら文章（本文より小さい字の参考文献・謝辞は段落の行に数えないので境界にならない。Kotake 2013 Fig.16）
    side = (clip[2], wide[1], wide[2], wide[3]) if cap["layout"] == "left" else (wide[0], wide[1], clip[0], wide[3])
    long_lines = sum(ln["bbox"][2] - ln["bbox"][0] > 0.2 * page.rect.width
                     for b in page.get_text("dict", clip=pymupdf.Rect(side))["blocks"] for ln in b.get("lines", []))
    if long_lines >= 3:
        return keep
    return wide, wide_region, "full"
