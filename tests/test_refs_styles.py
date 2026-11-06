"""参考文献の書き方（見出し・番号・著者年）と引用の書き方のテスト。前半は PDF なし、後半は棚の実物（無ければ飛ばす）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pfe import cite as C  # noqa: E402
from pfe.abstract import _join, extract_abstract  # noqa: E402


def test_superscript_needs_pdf_superscript():
    tokens = {"58", "59,60", "2"}
    assert C._superscript_cites("of a plant cell wall,58 the analysis", tokens, 70) == [58]
    assert C._superscript_cites("a single event,59,60 and thus", tokens, 70) == [59, 60]
    assert C._superscript_cites("the value 58 was", tokens, 70) == []          # 前に空白: 引用でない
    assert C._superscript_cites("released CO2 and 5 m2 of", tokens, 70) == []  # 化学式・単位
    assert C._superscript_cites("wall,99 the", {"99"}, 70) == []               # 一覧より大きい番号
    assert C._superscript_cites("≈1.5 ps and 42.5 Å", {"5"}, 70) == []         # 小数


def test_paren_cites():
    assert C._paren_cites("aggregation (Fig. 1B) (22) and (24, 25) or (140 °C)", 40) == [22, 24, 25]
    assert C._paren_cites("as in (1−3)", 40) == [1, 2, 3]


def test_author_year_cites():
    entries = [{"num": 1, "raw": "Capanema, E., 2001. Title.", "_key": C._letters("Capanema, E., 2001")},
               {"num": 2, "raw": "Wang, J., Minami, E., 2023a. A.", "_key": C._letters("Wang, J., Minami")},
               {"num": 3, "raw": "Wang, J., Minami, E., 2023b. B.", "_key": C._letters("Wang, J., Minami")},
               {"num": 4, "raw": "Van Aelst, K., 2020. C.", "_key": C._letters("Van Aelst, K")}]
    s = "emerged (Capanema et al., 2001; Wang et al., 2023b) and Van Aelst et al. (2020), not Smith et al., 1999."
    assert C._author_year_cites(s, entries) == [1, 3, 4]


def test_true_hyphen_is_kept():
    assert _join(["the water-", "rich and bio-", "mass"], {"water-rich"}) == "the water-rich and biomass"


def _shelf(pid):
    d = ROOT / "work" / pid
    if not (d / "source.pdf").is_file():
        pytest.skip(f"{pid} が無い")
    doc = pymupdf.open(d / "source.pdf")
    return doc, json.loads((d / "text.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("pid, style, n_min, fig", [
    ("mostofian2016_jacs", "numeric", 60, "fig3"),      # ACS: "■ REFERENCES"・"(1)"・上付きの引用
    ("pingali2020_pnas", "numeric", 30, "fig1"),        # PNAS: 見出しなし・"1."・"(22)" の引用
    ("miyamoto2025_gasification", "numeric", 10, None),  # JJAP: 見出しなし・"1)"
    ("wang2026_pyrolysis", "author_year", 30, "fig3"),  # Elsevier の著者年
    ("wang2024_anisotropy", "author_year", 30, None),   # Springer の著者年
])
def test_shelf_reference_styles(pid, style, n_min, fig):
    doc, text = _shelf(pid)
    r = C.parse_references(doc, text)
    assert r["style"] == style and len(r["entries"]) >= n_min
    assert [e["num"] for e in r["entries"]] == list(range(1, len(r["entries"]) + 1))
    assert all(c["num"] <= len(r["entries"]) for v in r["by_figure"].values() for c in v)
    if fig:
        assert r["by_figure"].get(fig)


def test_page_footer_is_not_in_entries():
    """ACS の脚（ページ番号・Article・論文自身の DOI）を項目に混ぜない。"""
    doc, text = _shelf("mostofian2016_jacs")
    r = C.parse_references(doc, text)
    assert all(e["doi"] != "10.1021/jacs.6b03285" for e in r["entries"])
    assert r["entries"][-1]["raw"].endswith("379, 55−57.")


def test_author_year_entries_are_whole():
    """1 項目ずつに分かれ、著者と年で始まる（ページの柱や謝辞を混ぜない）。"""
    doc, text = _shelf("wang2026_pyrolysis")
    r = C.parse_references(doc, text)
    wang = next(e for e in r["entries"] if e["raw"].startswith("Wang, J., Minami, E., Kawamoto, H., 2023a."))
    assert wang["doi"] == "10.1039/d2gc03719j" and wang["label"] == "Wang 2023a"
    assert not [e for e in r["entries"] if "Bioresource Technology 442" in e["raw"]]


def test_pnas_abstract_without_heading():
    doc, _ = _shelf("pingali2020_pnas")
    a = extract_abstract(doc)
    assert a["text"].startswith("A particularly promising") and a["text"].endswith("at the macroscopic scale.")
    assert "water-rich" in a["text"] and "molecular-dynamics" in a["text"] and "fractionating" in a["text"]
