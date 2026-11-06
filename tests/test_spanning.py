"""段抜きの図（短い題で片方の段と判定される）と、続きのページの図。前半は人工の PDF、後半は棚の実物（無ければ飛ばす）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pfe import figures as F  # noqa: E402

BODY = "Body text of the paper continues across this column here."


def _page(doc, cols=True):
    """本文 10pt の 2 段のページ。y=300 まで本文で埋める。"""
    page = doc.new_page(width=600, height=800)
    if cols:
        for x in (40, 310):
            for i in range(20):
                page.insert_text((x, 70 + i * 12), BODY, fontsize=10)
    return page


def _block(page, rect):
    page.draw_rect(pymupdf.Rect(rect), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))


def _by_id(doc):
    return {f["id"]: f for f in F.detect_figures(doc)}


def test_full_width_figure_with_short_caption():
    """短い題でも、墨が段の境界をまたぐ図は全幅で取る（旧: 左半分で切った。Yao 2024 Fig.1）。"""
    doc = pymupdf.open()
    page = _page(doc)
    _block(page, (45, 330, 555, 520))
    page.insert_text((45, 540), "Fig. 1 Short caption", fontsize=8)
    f = _by_id(doc)["fig1"]
    assert f["clip"][2] > 550 and f["layout"] == "full"


def test_column_figure_stays_in_its_column():
    doc = pymupdf.open()
    page = _page(doc)
    _block(page, (45, 330, 280, 520))
    page.insert_text((45, 540), "Fig. 1 Short caption", fontsize=8)
    f = _by_id(doc)["fig1"]
    assert f["clip"][2] < 290 and f["layout"] == "left"


def test_side_by_side_figures_are_not_merged():
    """左右に並ぶ 2 枚は、左の絵が段の境界に届いていても全幅にしない。"""
    doc = pymupdf.open()
    page = _page(doc)
    _block(page, (45, 330, 302, 520))
    _block(page, (312, 330, 555, 520))
    page.insert_text((45, 540), "Fig. 1 Left panel", fontsize=8)
    page.insert_text((312, 540), "Fig. 2 Right panel", fontsize=8)
    figs = _by_id(doc)
    assert figs["fig1"]["clip"][2] < 310 and figs["fig2"]["clip"][0] > 290


def test_neighbor_figure_with_lower_caption_is_not_merged():
    """隣の段の図の題がもっと下にあっても抱き込まない（Fukutome 2016 Fig.5 と Fig.4）。"""
    doc = pymupdf.open()
    _page(doc)                       # 本文の字の大きさを決めるページ
    page = _page(doc, cols=False)
    _block(page, (45, 70, 290, 500))
    page.insert_text((45, 520), "Fig. 4 Tall figure on the left", fontsize=8)
    _block(page, (302, 70, 555, 300))
    page.insert_text((302, 320), "Fig. 5 Shorter on the right", fontsize=8)
    f = _by_id(doc)["fig5"]
    assert f["clip"][0] > 290


def test_neighbor_text_is_not_absorbed():
    """隣の段の本文より小さい字（参考文献）を全幅に巻き込まない（Kotake 2013 Fig.16）。"""
    doc = pymupdf.open()
    page = _page(doc, cols=False)
    for i in range(30):
        page.insert_text((40, 70 + i * 12), BODY, fontsize=10)
    page2 = doc.new_page(width=600, height=800)
    _block(page2, (45, 70, 300, 300))
    page2.insert_text((45, 320), "Fig. 2 Left column figure", fontsize=8)
    for i in range(20):
        page2.insert_text((310, 70 + i * 10), "[1] Reference entry in small type, long line.", fontsize=7)
    f = _by_id(doc)["fig2"]
    assert f["clip"][2] <= 304        # 段の端（中央＋3pt）で止まる


def _continued_doc(line="Fig. 3. (continued)."):
    doc = pymupdf.open()
    p1 = _page(doc)
    _block(p1, (45, 330, 555, 600))
    p1.insert_text((45, 620), "Fig. 3. Long title of the figure.", fontsize=8)
    p2 = _page(doc, cols=False)
    _block(p2, (45, 70, 555, 400))
    p2.insert_text((260, 420), line, fontsize=8)
    return doc


def test_continued_figure_is_its_own_entry():
    figs = _by_id(_continued_doc())
    c = figs["fig3-cont"]
    assert c["page"] == 2 and c["continues"] == "fig3" and c["label"] == "Fig. 3（続き）"
    assert c["caption"] == "Fig. 3. Long title of the figure. (continued)"
    assert c["clip"][1] < 75 and c["clip"][3] < 415 and c["clip"][2] > 550
    assert "continues" not in figs["fig3"]
    # 親のあとに並ぶ（player の順番）
    assert [f["id"] for f in F.detect_figures(_continued_doc())] == ["fig3", "fig3-cont"]


def test_continued_without_parent_is_ignored():
    """親のキャプションが前のページに無い続きの行は項目にしない。"""
    doc = pymupdf.open()
    page = _page(doc, cols=False)
    _block(page, (45, 70, 555, 400))
    page.insert_text((260, 420), "Fig. 9. (continued).", fontsize=8)
    page.insert_text((40, 500), BODY, fontsize=10)
    assert "fig9-cont" not in _by_id(doc)


# --- 実物（棚の論文） ------------------------------------------------------------

def _shelf(pid):
    path = ROOT / "work" / pid / "source.pdf"
    if not path.is_file():
        pytest.skip(f"{pid} が無い")
    return _by_id(pymupdf.open(path))


def test_yao_fig1_full_width():
    """Yao 2024 Fig.1: 題の幅 140pt・墨は x=42〜553。右下のラベル（y≒700）まで入れる。"""
    f = _shelf("yao2024_process")["fig1"]
    assert f["clip"][2] > 550 and f["clip"][3] > 700 and f["clip"][1] > 505


@pytest.mark.parametrize("pid, fid, page, ymax", [("wang2025_metal", "fig7", 8, 490), ("wang2026_pyrolysis", "fig3", 6, 485)])
def test_shelf_continued(pid, fid, page, ymax):
    figs = _shelf(pid)
    c = figs[f"{fid}-cont"]
    assert c["page"] == page and c["continues"] == fid and c["found"] == "above" and c["confidence"] == "high"
    assert c["clip"][3] <= ymax and c["clip"][3] - c["clip"][1] > 400
    assert c["caption"] == figs[fid]["caption"] + " (continued)"
