"""本文の字の大きさを 1 つに決めて本文を失った 3 つの型と、ローマ数字の言及（2026-11-06）。"""
import pymupdf
import pytest

from pfe.text import extract_text, find_fig_mentions

LOREM = "The solvent changes the structure of lignin in the mixture and this sentence fills the line well. "


def _page(doc, size, heading=None, n_lines=40):
    """同じ大きさの行で埋めたページを足す。行は幅いっぱいにして、段落の切れ目を作らない。"""
    page = doc.new_page(width=595, height=792)
    y = 70
    if heading:
        page.insert_text((50, y), heading, fontsize=size, fontname="hebo")
        y += size + 4
    for _ in range(n_lines):
        page.insert_text((50, y), LOREM[:90], fontsize=size)
        y += size + 2.5
        if y > 720:
            break
    return page


def _chars(text):
    return sum(len(p["text"]) for p in text["paragraphs"])


def test_参考文献の方が多くても本文を失わない():
    # Bakulin 2024: 本文 8.5pt より参考文献 7pt の方が多く、本文が全部消えた
    doc = pymupdf.open()
    _page(doc, 8.5)
    _page(doc, 7.0, heading="References", n_lines=90)
    _page(doc, 7.0, n_lines=90)
    text = extract_text(doc, [])
    assert {p["page"] for p in text["paragraphs"]} == {1}
    assert _chars(text) > 2000


def test_ページごとに字の大きさが違っても全ページを拾う():
    # Jakab 1997（走査 PDF の OCR）: 奇数ページが 9.9pt、偶数ページが 10.6pt で半分消えた
    doc = pymupdf.open()
    for i in range(4):
        _page(doc, 9.5 if i % 2 else 10.6)
    # 段落の page は始まりのページなので、全ページの字が入ったかは字数で見る
    in_pdf = sum(len(p.get_text().replace("\n", "")) for p in doc)
    assert _chars(extract_text(doc, [])) > 0.95 * in_pdf


def test_図の枠の中の別の大きさの文字は本文にしない():
    doc = pymupdf.open()
    _page(doc, 10.0)
    _page(doc, 8.5)
    fig = {"id": "fig1", "page": 2, "clip": [0, 0, 595, 792], "caption_bbox": [0, 0, 1, 1], "layout": "column", "caption": "x"}
    assert {p["page"] for p in extract_text(doc, [fig])["paragraphs"]} == {1}


@pytest.mark.parametrize("sentence, expected", [
    ("Table I11 lists seven solvents.", ["table3"]),
    ("As shown in Tables II and III, the yield rose.", ["table2", "table3"]),
    ("TABLE VI summarizes the data.", ["table6"]),
    ("See Tab. 3 for the values.", ["table3"]),
    ("Figure l shows the spectrum.", ["fig1"]),
    ("Fig. 5a and Figures 2–4 are similar.", ["fig5", "fig2", "fig3", "fig4"]),
    ("Type I and Lignin I are not figures.", []),
])
def test_図表の言及(sentence, expected):
    assert find_fig_mentions(sentence) == expected
