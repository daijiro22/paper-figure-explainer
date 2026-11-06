"""表の検出と、表の幾何（pfe/tables.py）のテスト。合成した PDF で確かめる。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pymupdf = pytest.importorskip("pymupdf")
from pfe.figures import detect_figures  # noqa: E402
from pfe.tables import table_geometry  # noqa: E402

ROWS = [["Sample", "Na+", "K+", "Lignin (%)"],
        ["Cedar H-form", "0.5", "n.d.", "27.6"],
        ["Cedar Na-form", "43.1", "1.0", "27.1"],
        ["Beech H-form", "0.4", "0.0", "17.0"]]


def _pdf(path: Path) -> Path:
    """本文 10pt の段落の下に、本文と同じ 10pt の「Table 1.」の題（ACS 型）と、罫線で区切った 4 行 4 列の表。"""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for i in range(12):                                  # 本文（本文の字の大きさを 10pt に決めるため多めに）
        page.insert_text((60, 80 + 14 * i), "Body text of the paper continues here with enough words.", fontsize=10)
    page.insert_text((60, 300), "Table 1. Metal cation contents of wood samples", fontsize=10)
    x = [60, 200, 280, 360, 470]
    y = [312, 332, 352, 372, 392]
    for yy in y:
        page.draw_line((x[0], yy), (x[-1], yy))
    for xx in x:
        page.draw_line((xx, y[0]), (xx, y[-1]))
    for r, row in enumerate(ROWS):
        for c, cell in enumerate(row):
            page.insert_text((x[c] + 4, y[r] + 14), cell, fontsize=9)
    doc.save(path)
    return path


def test_body_size_table_caption_and_clip(tmp_path):
    figs = detect_figures(pymupdf.open(_pdf(tmp_path / "t.pdf")))
    t = [f for f in figs if f["id"] == "table1"]
    assert t, "本文と同じ大きさの Table 1. の題と、その下の罫線の表を見つける"
    t = t[0]
    assert t["kind"] == "table" and t["found"] == "table" and t["confidence"] == "high"
    x0, y0, x1, y1 = t["clip"]
    assert x0 <= 60 and x1 >= 470 and y0 <= 312 and y1 >= 392       # 表の外枠が入る
    assert y0 > 300                                                # 題の行は入れない
    assert not [f for f in figs if f["id"].startswith("fig")]      # 本文の行を図と取り違えない


def test_table_geometry_cells_and_text(tmp_path):
    page = pymupdf.open(_pdf(tmp_path / "t.pdf"))[0]
    g = table_geometry(page, (55, 308, 475, 396))
    assert g["kind"] == "table" and g["panels"] == [] and g["table_unit"] == "セル"
    assert len(g["regions"]) == 16 and g["regions"][0]["id"] == "G1"
    lines = g["table_text"].split("\n")
    assert lines[0] == "Sample Na+ K+ Lignin (%)"
    assert lines[2] == "Cedar Na-form 43.1 1.0 27.1"                 # 小数がそのまま残る（照合に使える）
    assert table_geometry(page, (500, 500, 590, 590)) is None        # 文字の無い所（画像の表として扱う）
