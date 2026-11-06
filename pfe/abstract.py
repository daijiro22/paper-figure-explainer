"""論文のアブストラクトを PDF の最初の数ページから取り出す（概要の解説の原文・根拠に使う）。

本文の取り出し（pfe/text.py）は本文の大きさの行だけを拾うので、本文より小さい字のアブストラクト
（Elsevier の「A B S T R A C T」の囲み）を落とす。ここでは本文とは別に、見出しの位置から範囲を決めて取る:
  1. 見出し: 字間を空けた「A B S T R A C T」も含めて、空白を詰めて "abstract" で始まる行。
     「ABSTRACT: …」「Abstract The …」のように見出しと本文が同じ行のものも扱う
  2. 範囲: 見出しと同じ段（左端がそろう）・同じ字の大きさ・詰まった行間で続く行。
     キーワード・Introduction・論文情報（受付日・©）が来たら止める（隣の ARTICLE INFO の段は混ぜない）
"""
from __future__ import annotations

import re

STOP = re.compile(r"^\s*(?:■\s*)?(?:\d+\.?\s*)?(?:introduction|keywords?|key\s*words|highlights|graphical\s*abstract|"
                  r"received|accepted|available\s*online|article\s*history|©|copyright|https?://|doi:)", re.I)
INLINE = re.compile(r"^\s*abstract\s*[:.\-–—]?\s*", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _hyphen_words(doc) -> set[str]:
    """文書の中で 1 行に収まっている「語-語」（本当のハイフン）。行末で切れた "water-" "rich" を "waterrich" にしないために使う。"""
    out: set[str] = set()
    for page in doc:
        out.update(w.lower() for w in re.findall(r"[A-Za-z]+-[A-Za-z]+", page.get_text()))
    return out


def _join(parts: list[str], keep: set[str] | None = None) -> str:
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out.endswith("\xad"):                   # 行末の柔らかいハイフン（Elsevier）: つなげる
            out = out[:-1] + p
        elif out.endswith("-") and p[:1].islower() and not out.endswith(" -"):
            tail, head = re.search(r"([A-Za-z]*)-$", out).group(1), re.match(r"[A-Za-z]*", p).group(0)
            # 行末で切れた語（"bio-" "mass"）はつなげる。本文に "water-rich" の形があれば本当のハイフンなので残す
            out = (out + p) if keep and f"{tail}-{head}".lower() in keep else out[:-1] + p
        else:
            out = (out + " " + p) if out else p
    return re.sub(r"\s+", " ", out.replace("\xad", "")).strip()


def extract_abstract(doc, max_pages: int = 3) -> dict | None:
    """{"text", "page", "bboxes", "method"}。見つからなければ None。"""
    from pfe.pdfutil import page_lines
    for pno in range(min(max_pages, doc.page_count)):
        lines = page_lines(doc[pno], pno + 1)
        for i, ln in enumerate(lines):
            n = _norm(ln["text"])
            if not n.startswith("abstract"):
                continue
            rest = INLINE.sub("", ln["text"], count=1)
            inline = len(n) > len("abstract") + 3       # 見出しと本文が同じ行
            body = ([ln] if inline else [])
            # 本文の行の基準: 見出しの次に来る、見出しと左端がそろう行
            below = [l for l in lines if l.y0 > ln.y0 + 1 and abs(l.x0 - ln.x0) < 40]
            if not inline and not below:
                continue
            ref = ln if inline else min(below, key=lambda l: l.y0)
            size, x0 = ref["size"], ref.x0
            prev = ln
            for l in sorted((l for l in lines if l.y0 > ln.y0 + 1), key=lambda l: l.y0):
                if l is ln:
                    continue
                if abs(l["size"] - size) > 0.4 or not (x0 - 4 <= l.x0 <= x0 + 40):
                    continue                            # 別の段（ARTICLE INFO・キーワードの段）や見出し
                h = max(1.0, prev.y1 - prev.y0)
                if l.y0 - prev.y1 > 1.6 * h:
                    break                               # 行間が空いた: アブストラクトの終わり
                if STOP.match(l["text"]):
                    break
                body.append(l)
                prev = l
            parts = [rest if (b is ln) else b["text"] for b in body]
            text = _join(parts, _hyphen_words(doc))
            if len(text) < 200:                         # 短すぎるものは見出しの言及など（"Abstract" だけの行）
                continue
            return {"text": text, "page": pno + 1, "bboxes": [list(map(lambda v: round(v, 1), b["bbox"])) for b in body],
                    "method": "inline" if inline else "heading"}
    return _unlabeled(doc, max_pages)


def _unlabeled(doc, max_pages: int) -> dict | None:
    """見出しの無いアブストラクト（RSC は著者名の下に囲みも見出しも無く置く）。
    Introduction の見出しより上にある、左端と字の大きさがそろい行間の詰まった 400 字以上の最初のかたまり。
    PNAS は Introduction の見出しも無いので、1 ページ目に限り、本文より小さい字の同じかたまりを探す。"""
    from pfe.pdfutil import body_font_size, page_lines
    hyphens = _hyphen_words(doc)
    for pno in range(min(max_pages, doc.page_count)):
        lines = sorted(page_lines(doc[pno], pno + 1), key=lambda l: l.y0)
        intro = next((l for l in lines if re.match(r"^\s*(?:■\s*)?(?:\d+\.?\s*)?introduction\s*$", l["text"], re.I)), None)
        if intro is None and pno > 0:
            continue
        if intro is None:
            body = body_font_size(doc)
            above = [l for l in lines if l["size"] < body - 0.3]
        else:
            above = [l for l in lines if l.y1 <= intro.y0 + 1]
        used: set[int] = set()
        for i, l in enumerate(above):
            if i in used or STOP.match(l["text"]):
                continue
            block, prev = [l], l
            for j in range(i + 1, len(above)):
                c = above[j]
                if abs(c.x0 - l.x0) > 4 or abs(c["size"] - l["size"]) > 0.3:
                    continue
                if c.y0 - prev.y1 > 1.6 * max(1.0, prev.y1 - prev.y0) or STOP.match(c["text"]):
                    break
                block.append(c)
                used.add(j)
                prev = c
            text = _join([b["text"] for b in block], hyphens)
            # Introduction の無いページでは、文が 3 つ以上あるものだけ（PNAS の所属の欄も同じ字でかたまる）
            if len(block) >= 4 and len(text) >= 400 and (intro is not None or len(re.findall(r"[a-z)]\.\s", text)) >= 3):
                return {"text": text, "page": pno + 1, "bboxes": [[round(v, 1) for v in b["bbox"]] for b in block],
                        "method": "unlabeled"}
    return None
