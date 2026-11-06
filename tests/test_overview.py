"""アブストラクトの取り出し（pfe/abstract.py）と、概要の台本の検査（pfe/overview.py）のテスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pymupdf = pytest.importorskip("pymupdf")
from pfe.abstract import extract_abstract  # noqa: E402
from pfe.overview import check  # noqa: E402

ABS = ["Understanding the thermal degradation of lignin in wood cell walls is essential.",
       "In this study, Japanese cedar wood was analysed by TG-MS at temperatures up to 1000 C,",
       "and six distinct peaks were observed. The beta-ether cleavage proceeded first, followed",
       "by side-chain saturation and demethylation. These results provide insights into the",
       "influence of cell wall ultrastructure on lignin pyrolysis reactions in wood samples."]


def _elsevier(path: Path) -> Path:
    """Elsevier 型の 1 ページ目: 左の段に A R T I C L E I N F O とキーワード、右の段に A B S T R A C T と本文より小さい字の要旨、
    その下に本文（10pt）の 1. Introduction。"""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=794)
    p.insert_text((38, 160), "A test paper title", fontsize=14)
    p.insert_text((38, 285), "A R T I C L E  I N F O", fontsize=7)
    p.insert_text((202, 285), "A B S T R A C T", fontsize=7)
    p.insert_text((38, 300), "Keywords:", fontsize=6.4)
    p.insert_text((38, 309), "Wood pyrolysis", fontsize=6.4)
    for i, t in enumerate(ABS):
        p.insert_text((202, 300 + 9.5 * i), t, fontsize=7.2)
    p.insert_text((38, 480), "1. Introduction", fontsize=8)
    for i in range(30):
        p.insert_text((38, 500 + 10 * i), "Body text of the paper continues with many words here.", fontsize=8)
    doc.save(path)
    return path


def test_elsevier_spaced_heading(tmp_path):
    a = extract_abstract(pymupdf.open(_elsevier(tmp_path / "e.pdf")))
    assert a and a["method"] == "heading" and a["page"] == 1
    assert a["text"].startswith("Understanding the thermal degradation")
    assert a["text"].endswith("in wood samples.")
    assert "Keywords" not in a["text"] and "Wood pyrolysis" not in a["text"] and "Introduction" not in a["text"]


def test_overview_check_finds_bad_quote_number_and_fig():
    abstract = " ".join(ABS)
    good = {"steps": [{"section": "目的", "say": "木の中のリグニンの熱分解を調べました。", "caption_html": "目的", "point": None,
                       "figs": [], "evidence": [{"source": "abstract", "quote": "Japanese cedar wood was analysed by TG-MS"}]},
                      {"section": "結果", "say": "1000 ℃までに 6 つの山が見えました。", "caption_html": "結果", "point": None,
                       "figs": ["fig1"], "evidence": [{"source": "abstract", "quote": "six distinct peaks were observed"}]}]}
    assert check(good, abstract, ["fig1", "fig2"]) == []
    bad = {"steps": [dict(good["steps"][0], evidence=[{"source": "abstract", "quote": "this sentence is not in the abstract"}]),
                     dict(good["steps"][1], say="1200 ℃まで調べました。", figs=["fig9"])]}
    errs = check(bad, abstract, ["fig1", "fig2"])
    assert any("見つからない" in e for e in errs)
    assert any("1200" in e for e in errs)
    assert any("fig9" in e for e in errs)
