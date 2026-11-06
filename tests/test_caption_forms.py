"""キャプションの形と本文サイズの判定（2026-11-05 の Luo 2011・Takada 2019・Smith 2016 PCCP の取りこぼし）。"""
import pymupdf

from pfe.figures import UPPER_CAPTION_RE
from pfe.pdfutil import CAPTION_RE, body_font_size


def _doc(sizes: dict[float, int]):
    """大きさごとに決まった字数の行を書いた PDF を作る。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 40
    for size, chars in sizes.items():
        for _ in range(chars // 50):
            page.insert_text((30, y), "x" * 50, fontsize=size)
            y += 10
    return doc


def test_body_prefers_larger_size_when_nearly_tied():
    # Luo 2011: 6.4pt（実験方法・文献）が 8pt（本文）をわずかに上回っていた
    assert body_font_size(_doc({6.4: 1000, 8.0: 950})) == 8.0


def test_body_keeps_most_common_size_when_not_tied():
    # 大きい字が明らかに少なければ、最多の字が本文
    assert body_font_size(_doc({8.0: 1000, 10.0: 300})) == 8.0


def test_caption_with_closing_parenthesis():
    # RSC の原稿の形
    assert CAPTION_RE.match("Figure 2) Distribution of Fractal Dimension")
    assert CAPTION_RE.match("Table 1) Kinetic constants")


def test_upper_case_caption_is_case_sensitive():
    # Wiley の新しい組版。本文の言及 "Figure 1 shows" は通さない
    assert UPPER_CAPTION_RE.match("FIGURE 1")
    assert UPPER_CAPTION_RE.match("TABLE 2 The yield")
    assert not UPPER_CAPTION_RE.match("Figure 1 shows")
