"""テキスト側（figures / text / cite / CLI）のテスト。実物のテスト論文（Taira 2026 JAAP）に対して実行する。"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import glob
import json
import re
import subprocess
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pfe import cite as C  # noqa: E402
from pfe import figures as F  # noqa: E402
from pfe import text as T  # noqa: E402
from pfe.pdfutil import all_lines, body_font_size  # noqa: E402

from _paths import local  # noqa: E402

_PDFS = glob.glob(local(r"Taira et al\Taira et al. - 2026*.pdf"))
pytestmark = pytest.mark.skipif(not _PDFS, reason="テスト論文が見つからない")


@pytest.fixture(scope="module")
def doc():
    return pymupdf.open(_PDFS[0])


@pytest.fixture(scope="module")
def figs(doc):
    return F.detect_figures(doc)


@pytest.fixture(scope="module")
def text(doc, figs):
    return T.extract_text(doc, figs)


@pytest.fixture(scope="module")
def refs(doc, text):
    return C.parse_references(doc, text)


def _contains(clip, rect):
    return clip[0] <= rect[0] and clip[1] <= rect[1] and clip[2] >= rect[2] and clip[3] >= rect[3]


# --- figures ---------------------------------------------------------------

def test_figure_ids(figs):
    ids = sorted(f["id"] for f in figs if f["id"].startswith("fig"))
    assert ids == sorted(f"fig{i}" for i in range(1, 14))
    assert len(ids) == len(set(ids))
    for f in figs:
        if f["id"].startswith("fig"):
            assert f["caption"].startswith(f"Fig. {f['number']}."), f["caption"][:30]
            assert f["page"] >= 1
    assert any(f["id"] == "table1" and f["kind"] == "table" for f in figs)


def test_fig2_found(figs):
    f2 = next(f for f in figs if f["id"] == "fig2")
    assert f2["page"] == 3 and f2["layout"] == "left"
    assert f2["caption"].startswith("Fig. 2.")


def test_fig1(figs, doc):
    f = next(f for f in figs if f["id"] == "fig1")
    assert f["page"] == 2 and f["layout"] == "right"
    assert _contains(f["clip"], [345, 620, 520, 712])
    assert f["clip"][3] < f["caption_bbox"][1]          # キャプションより上
    assert f["clip"][1] >= 600                          # 上の本文を含まない
    assert f["confidence"] == "high"


def test_fig13_raster(figs):
    f = next(f for f in figs if f["id"] == "fig13")
    assert f["page"] == 9
    assert _contains(f["clip"], [43, 55, 283, 210])


def test_no_body_text_in_clips(figs, doc):
    body = body_font_size(doc)
    lines = all_lines(doc)
    for f in figs:
        for l in lines[f["page"] - 1]:
            if abs(l["size"] - body) > 0.3:
                continue
            w = min(f["clip"][2], l.x1) - max(f["clip"][0], l.x0)
            h = min(f["clip"][3], l.y1) - max(f["clip"][1], l.y0)
            assert not (w > 3 and h > 3), f"{f['id']} が本文行 {l['text'][:30]!r} と重なる"


def test_override(tmp_path, figs):
    (tmp_path / "figures.json").write_text(json.dumps(figs), encoding="utf-8")
    (tmp_path / "figures_override.json").write_text(
        json.dumps([{"id": "fig1", "clip": [1, 2, 3, 4]}]), encoding="utf-8")
    out = F.load_figures(tmp_path)
    f1 = next(f for f in out if f["id"] == "fig1")
    assert f1["clip"] == [1, 2, 3, 4] and f1["source"] == "override"
    assert next(f for f in out if f["id"] == "fig2")["source"] == "auto"


def test_render_figure(doc, figs, tmp_path):
    f = next(f for f in figs if f["id"] == "fig1")
    scale = F.render_figure(doc, f, tmp_path / "fig1.png", long_edge=1800)
    pix = pymupdf.Pixmap(str(tmp_path / "fig1.png"))
    assert max(pix.width, pix.height) in range(1795, 1806)
    assert scale == pytest.approx(1800 / max(f["clip"][2] - f["clip"][0], f["clip"][3] - f["clip"][1]))


# --- text ------------------------------------------------------------------

def test_cross_page_sentence(text):
    hits = [s for p in text["paragraphs"] for s in p["sentences"] if "DTG peaks at 353" in s["text"]]
    assert len(hits) == 1
    assert hits[0]["figs"] == ["fig1"]
    assert hits[0]["text"].endswith("respectively.")
    assert "353 °C" in hits[0]["text"] and "◦" not in hits[0]["text"]


def test_fig_mentions_cover(text, figs):
    ids = [f["id"] for f in figs if f["id"].startswith("fig")]
    covered = [i for i in ids if text["fig_mentions"].get(i)]
    assert len(covered) >= 11, set(ids) - set(covered)


def test_no_caption_header_or_hyphen_artifacts(text):
    alltext = " ".join(p["text"] for p in text["paragraphs"])
    assert "D. Taira et al." not in alltext
    assert "\u00ad" not in alltext
    assert "softwood" in alltext and "soft wood" not in alltext
    assert "Temperature dependence of mass spectra" not in alltext      # Fig. 2 のキャプション


def test_sentence_split_abbreviations():
    s = T.split_sentences("MWL was prepared by Lundquist et al. [35], as in Fig. 3, e.g. J.E. White "
                          "used 0.25 mm sieves vs. 1 mm ones. Next sentence starts here.")
    assert len(s) == 2 and s[1].startswith("Next")


def test_find_fig_mentions():
    f = T.find_fig_mentions
    assert f("shown in Fig. 1, with the DTG peaks") == ["fig1"]
    assert f("Figs. 2 and 3 show") == ["fig2", "fig3"]
    assert f("see Fig. 5a and Figure S2") == ["fig5", "figS2"]
    assert f("Figures 2–4 compare") == ["fig2", "fig3", "fig4"]


def test_cite_ranges():
    assert T.find_cites("as reported [3–5].") == [3, 4, 5]
    assert T.find_cites("as in [2,6] and [7,9–11]") == [2, 6, 7, 9, 10, 11]
    assert T.find_cites("databases [33, 44].") == [33, 44]


# --- refs ------------------------------------------------------------------

def test_refs(doc, refs):
    n_entries = len(refs["entries"])
    assert n_entries > 30
    assert [e["num"] for e in refs["entries"]] == list(range(1, n_entries + 1))
    for e in refs["entries"]:
        if e["doi"]:
            assert not re.search(r"\s", e["doi"]), e["doi"]
            assert e["doi"].startswith("10.")
    # DOI が行折り返しで割れている項目が復元されている
    assert refs["entries"][1]["doi"] == "10.1016/j.biotechadv.2018.11.004"
    assert refs["entries"][6]["doi"] == "10.1016/j.jaap.2008.06.003"
    # 文献リストの [n] 項目数と一致（本文末尾の "[n] " 行頭を数える）
    pages = "\n".join(doc[i].get_text() for i in range(len(doc)))
    tail = pages[pages.rindex("References"):]
    assert n_entries == len(re.findall(r"(?m)^\[\d+\] ", tail))


def test_by_figure_only_nearby_sentences(text, refs):
    # by_figure の各引用は、図に言及した文かその直後の文のものだけ
    sent = {(p["id"], s["i"]): s for p in text["paragraphs"] for s in p["sentences"]}
    for fid, items in refs["by_figure"].items():
        for it in items:
            s = sent[(it["para"], it["sentence"])]
            assert it["num"] in s["cites"]
            prev = sent.get((it["para"], it["sentence"] - 1))
            assert fid in s["figs"] or (prev and fid in prev["figs"])


# --- CLI -------------------------------------------------------------------

def test_cli_offline(tmp_path):
    work = tmp_path / "w"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "extract_text.py"),
                        "--pdf", _PDFS[0], "--work", str(work)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    for name in ("figures.json", "text.json", "refs.json", "meta.json", "source.pdf"):
        assert (work / name).exists(), name
    meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    assert meta["doi"] == "10.1016/j.jaap.2026.107638" and meta["year"] == 2026
    assert "Daijiro Taira" in meta["authors"]
