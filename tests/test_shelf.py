"""棚のカードの論文情報: 雑誌の略称・著者の出し方・Crossref と人の修正の優先順。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import fill_biblio as fb  # noqa: E402
from pfe.meta import paper_meta, write_override  # noqa: E402
from pfe.biblio import clean_doi  # noqa: E402
from pfe.shelf import author_line, clean_journal, family_names, journal_short, known_journal, shelf_info  # noqa: E402


@pytest.mark.parametrize("raw, want", [
    ("Journal of Wood Science, doi:10.1007/s10086-016-1606-z", "Journal of Wood Science"),
    ("Biomass Conversion and Biorefinery, https://doi.org/10.1007/s13399-022-02387-3", "Biomass Conversion and Biorefinery"),
    ("RSC Advances (2024)", "RSC Advances"),
    ("RSC Sustainability (2023), 1, 1192-1199, doi:10.1039/D3SU00128H", "RSC Sustainability"),
    ("Paper Trade Journal 122(2), 35-43", "Paper Trade Journal"),
    ("hfsg", "Holzforschung"),         # Crossref の雑誌名が内部コードのもの
    ("Composites Part A: Applied Science and Manufacturing", "Composites Part A: Applied Science and Manufacturing"),
    (None, ""),
])
def test_clean_journal(raw, want):
    assert clean_journal(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Journal of the American Chemical Society", "J. Am. Chem. Soc."),
    ("JOURNAL OF WOOD SCIENCE", "J. Wood Sci."),                                # 大文字でも引ける
    ("Energy & Fuels", "Energy Fuels"),
    ("Industrial Crops & Products", "Ind. Crops Prod."),                        # & と and の表記ゆれ
    ("Industrial Crops and Products", "Ind. Crops Prod."),
    ("Composites Part A: Applied Science and Manufacturing", "Compos. Part A Appl. Sci. Manuf."),
    ("RSC Advances (2025)", "RSC Adv."),
    ("Proceedings of the National Academy of Sciences", "PNAS"),
    ("Holzforschung", "Holzforschung"),
    ("ACS Sustainable Chemistry & Engineering", "ACS Sustainable Chem. Eng."),  # 出版社の登録どおり
    ("Biomass Conversion and Biorefinery, https://doi.org/10.1007/x", "Biomass Conv. Bioref."),
])
def test_journal_short(raw, want):
    assert journal_short(raw) == want and known_journal(raw)


def test_unknown_journal_stays_full_not_guessed():
    assert journal_short("Journal of Something New, doi:10.1/x") == "Journal of Something New"
    assert not known_journal("Journal of Something New")


def test_author_line_shows_all_when_few_and_first_three_and_last_when_many():
    assert author_line(["Agarwal"]) == "Agarwal"
    assert author_line(["A", "B", "C", "D"]) == "A, B, C, D"
    assert author_line(["Meng", "Pu", "Sannigrahi", "Li", "Cao", "Ragauskas"]) == "Meng, Pu, Sannigrahi … Ragauskas"
    assert author_line([]) == ""


def test_family_names_prefer_crossref_and_fall_back_to_pdf_names():
    assert family_names({"authors": ["x y"], "authors_family": ["del Seppia", ""]}) == ["del Seppia"]
    assert family_names({"authors": ["Umesh P. Agarwal", " Xiankun Huang", "", "  "]}) == ["Agarwal", "Huang"]
    assert family_names({"authors": ["山田 太郎"]}) == ["山田"]      # 日本語名は姓が先
    assert family_names({}) == []


def test_shelf_info_makes_search_find_hidden_forms():
    s = shelf_info({"journal": "Journal of the American Chemical Society", "year": 2016,
                    "authors": ["Barmak Mostofian", "Charles M. Cai"], "authors_family": ["Mostofian", "Cai"]})
    assert (s["year"], s["journal_short"], s["authors_line"], s["first_author"]) == (2016, "J. Am. Chem. Soc.", "Mostofian, Cai", "Mostofian")
    for form in ("Journal of the American Chemical Society", "J. Am. Chem. Soc.", "J Am Chem Soc", "Cai"):
        assert form in s["search"]


def test_shelf_info_is_empty_safe():
    s = shelf_info({})
    assert s["year"] == "" and s["journal_short"] == "" and s["authors_line"] == "" and s["first_author"] == ""


def _paper(tmp_path: Path) -> Path:
    d = tmp_path / "p"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"title": "題", "authors": ["Pdf Name"], "journal": "J, doi:1", "year": 1910, "doi": "10.1/a"}), encoding="utf-8")
    return d


def test_paper_meta_prefers_crossref_then_human(tmp_path):
    d = _paper(tmp_path)
    assert paper_meta(d)["year"] == 1910                      # biblio.json が無ければ PDF の値
    (d / "biblio.json").write_text(json.dumps({"title": "Other Title", "year": 2005, "journal": "Holzforschung", "doi": "10.1/b",
                                               "authors": [{"family": "Lawoko", "given": "M."}, {"family": "Sole", "given": ""}]}), encoding="utf-8")
    m = paper_meta(d)
    assert (m["year"], m["journal"], m["doi"]) == (2005, "Holzforschung", "10.1/b")
    assert m["authors"] == ["M. Lawoko", "Sole"] and m["authors_family"] == ["Lawoko", "Sole"]
    assert m["title"] == "題"                                 # 題は Crossref で置き換えない
    write_override(d, authors=["P. Whiting", "D. A. I. Goring"], year=1981, journal="人が直した雑誌")
    m = paper_meta(d)
    assert (m["year"], m["journal"]) == (1981, "人が直した雑誌")
    assert m["authors"] == ["P. Whiting", "D. A. I. Goring"] and "authors_family" not in m      # 崩れた Crossref の姓を残さない
    assert family_names(m) == ["Whiting", "Goring"]


def test_paper_meta_ignores_empty_crossref_values(tmp_path):
    d = _paper(tmp_path)
    (d / "biblio.json").write_text(json.dumps({"year": None, "journal": "", "authors": []}), encoding="utf-8")
    m = paper_meta(d)
    assert (m["year"], m["authors"]) == (1910, ["Pdf Name"])


def test_clean_doi_drops_supporting_info_suffix():
    assert clean_doi("10.1021/acs.energyfuels.7b00110.s001") == "10.1021/acs.energyfuels.7b00110"
    assert clean_doi("10.1073/pnas.1/-/DCSupplemental") == "10.1073/pnas.1"
    assert clean_doi("10.1039/x0xx00000x") is None


def test_lookup_refuses_a_different_paper_unless_a_person_checked(tmp_path, monkeypatch):
    d = _paper(tmp_path)
    crossref = {"title": "A completely different study of something", "authors": [{"family": "X", "given": ""}], "journal": "J", "year": 2000}
    monkeypatch.setattr(fb.N, "crossref_by_doi", lambda doi, cache: crossref)
    bib, why = fb.lookup(d)
    assert bib is None and "題が違う" in why
    bib, why = fb.lookup(d, trust_doi=True)
    assert bib["year"] == 2000 and why == ""


def test_lookup_accepts_same_title_ignoring_case_and_symbols(tmp_path, monkeypatch):
    d = _paper(tmp_path)
    write_override(d, title="Structural Differences: the Lignin-Carbohydrate Complexes")
    monkeypatch.setattr(fb.N, "crossref_by_doi", lambda doi, cache: {"title": "Structural differences the lignin carbohydrate complexes",
                                                                     "authors": [], "journal": "J", "year": 2005})
    bib, why = fb.lookup(d)
    assert bib and bib["doi"] == "10.1/a" and bib["found_by"] == "DOI" and why == ""


def test_lookup_finds_by_title_when_no_doi_and_uses_main_record(tmp_path, monkeypatch):
    d = tmp_path / "q"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"title": "Fast Pyrolysis of Bark", "authors": []}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(fb.N, "doi_from_title", lambda title, cache: "10.1021/a.7b1.s001")

    def by_doi(doi, cache):
        seen["doi"] = doi
        return {"title": "Fast Pyrolysis of Bark", "authors": [], "journal": "Energy & Fuels", "year": 2017}
    monkeypatch.setattr(fb.N, "crossref_by_doi", by_doi)
    bib, _ = fb.lookup(d)
    assert seen["doi"] == "10.1021/a.7b1" and bib["found_by"] == "題"     # 補足資料でなく本体を引く


def test_lookup_reports_when_nothing_found(tmp_path, monkeypatch):
    d = tmp_path / "r"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"title": "Old Paper"}), encoding="utf-8")
    monkeypatch.setattr(fb.N, "doi_from_title", lambda title, cache: None)
    bib, why = fb.lookup(d)
    assert bib is None and "見つからない" in why
