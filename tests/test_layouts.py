"""出版社ごとの組版の違い（キャプションの書き方・行の割れ方・図の置き方）のテスト。

前半は PDF なしで動く単体テスト。後半は Mendeley にある実物（Springer・RSC）で、無ければ飛ばす。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pfe.pdfutil import CAPTION_RE, Line, _merge_same_row  # noqa: E402


# --- キャプションの書き出し ---------------------------------------------------

@pytest.mark.parametrize("text, num", [
    ("Fig. 1. TG and DTG curves", "1"),          # Elsevier
    ("Figure 3. PCA of SPI mass spectra", "3"),  # ACS
    ("Fig. 2: Structures", "2"),
    ("Fig. 1   Lignin (De–alkaline) sample", "1"),  # Springer（句点なし・細い空白）
    ("Fig. 1 Proposed scheme for production", "1"),                # RSC（句点なし）
    ("Fig. 2", "2"),                                               # RSC の札だけの行
    ("Fig. 2 In situ assessment of lignin", "2"),                  # 前置詞で始まるキャプション
    ("Table 1 As-received samples", "1"),
    ("Fig. 6   a–c P(u)/P(u0.5) versus α", "6"),                   # パネル記号で始まる
    ("Table I. Yields of polyols", "I"),                           # JJAP（ローマ数字）
    ("Table II. Experimental product gas", "II"),
])
def test_caption_re_accepts(text, num):
    m = CAPTION_RE.match(text)
    assert m and m.group(2) == num


@pytest.mark.parametrize("text", [
    "Fig. 5 shows the gas products",             # 本文の言及
    "Figure 2 is the TG–DTG–DSC curve",
    "Fig.   3 presents the results",
    "Fig. 4a to guaiacol, and methyl",           # 番号に字が続く
    "Fig. 6a–c that the experimental curves",
    "Figures 2 and 3",
    "Table I(a) presents the total yield",       # ローマ数字の言及
    "Table in the appendix",                     # 小文字はローマ数字とみなさない
])
def test_caption_re_rejects(text):
    assert not CAPTION_RE.match(text)


def test_roman_numbers_become_arabic_ids():
    """"Table II" は id table2・番号 2 にそろえる（並べ替えで I と II が同じ 0 にならない）。"""
    from pfe.figures import _arabic
    assert [_arabic(s) for s in ("I", "II", "IV", "IX", "XII", "3", "S2")] == ["1", "2", "4", "9", "12", "3", "S2"]


# --- 同じ高さの行をつなぐ ------------------------------------------------------

def _ln(x0, y0, x1, y1, text, size=8.0):
    span = {"text": text, "size": size, "flags": 0, "font": "Regular"}
    return Line(page=1, bbox=(x0, y0, x1, y1), text=text, size=size, bold=False, italic=False, spans=[span])


def test_merge_same_row_across_rounding_boundary():
    """RSC: "Fig. 2" の札（y0=670.54）と右の本文（y0=670.2）は round(y0) が違っても 1 行になる。"""
    lines = [_ln(329, 670.2, 553, 677.5, "Gel-permeation chromatograms"),
             _ln(303, 670.54, 321, 677.3, "Fig. 2", 7.5),
             _ln(43, 670.6, 292, 678.0, "left column text", 9.0)]
    out = _merge_same_row(lines)
    texts = [l["text"] for l in out]
    assert "Fig. 2 Gel-permeation chromatograms" in texts
    assert "left column text" in texts        # 段の間（11pt 以上）は越えない
    assert len(out) == 2


# --- 実物の PDF（Mendeley にあるときだけ） -----------------------------------

import functools  # noqa: E402
import glob  # noqa: E402

import pymupdf  # noqa: E402

from pfe import figures as F  # noqa: E402
from _paths import local  # noqa: E402

_MEN = {
    "springer_side": r"Huang et al\Huang et al. - 2024 - Pyrolysis of lignin*.pdf",
    "springer_text": r"Kawamoto\Kawamoto - 2017 - Lignin pyrolysis reactions.pdf",
    "rsc": r"Wang, Minami, Kawamoto\Wang, Minami, Kawamoto - 2023 - Effects of solvent*.pdf",
    "acs": r"Jia et al\Jia et al. - 2017 - Fast Pyrolysis*.pdf",
    "wiley_side": r"Du, Gellerstedt, Li\Du, Gellerstedt, Li - 2013 - Universal fractionation*.pdf",
    "rsc_masthead": r"Sapouna, Lawoko\Sapouna, Lawoko - 2021 - Deciphering*.pdf",
    # 2026-11-02 の切り出しの監査の型 1〜3
    "jjap_a4": r"Miyamoto, Minami, Kawamoto\Miyamoto, Minami, Kawamoto - 2025 - Gasification of aliphatic*.pdf",
    "jaap_flat": r"Nomura, Minami, Kawamoto\Nomura, Minami, Kawamoto - 2025 - High-speed camera*.pdf",
    "springer_side_stack": r"Wang et al\Wang et al. - 2024 - Thermogravimetric investigation*.pdf",
    "rsc_fig_under_table": r"Yao, Minami, Kawamoto\Yao, Minami, Kawamoto - 2024 - Energy Advances*.pdf",
}


def _figs(key):
    paths = glob.glob(local(_MEN[key]))
    if not paths:
        pytest.skip(f"{key} の PDF が無い")
    return _detect(paths[0])


@functools.lru_cache(maxsize=None)
def _detect(path):
    """同じ PDF の図の検出は 1 回だけ（Kawamoto 16 頁の検出・本文抽出が重く、全体で 5 分を超えた）。"""
    doc = pymupdf.open(path)
    return doc, {f["id"]: f for f in F.detect_figures(doc)}


def test_springer_side_caption():
    """Springer: 句点なしのキャプションを拾い、左の細い段にあるキャプションの右側を図として切る。"""
    doc, figs = _figs("springer_side")
    assert {f"fig{i}" for i in range(1, 8)} <= set(figs)
    f2 = figs["fig2"]
    assert f2["found"] == "right" and f2["confidence"] == "high"
    assert f2["clip"][0] > f2["caption_bbox"][2]          # 図はキャプションの右
    assert f2["clip"][2] - f2["clip"][0] > 300
    assert "1.8x10" not in figs["fig5"]["caption"]        # 軸の目盛りをキャプションに混ぜない
    assert f2["clip"][3] < 735                            # ページ下の "1 3"（Springer の印）を入れない


def test_springer_real_text_in_figure():
    """図の中に本文と同じ大きさの実テキストがあっても、図の上端をラベルの下に置かない（旧: 14pt の帯）。"""
    doc, figs = _figs("springer_text")
    assert len([k for k in figs if k.startswith("fig")]) == 11
    f8 = figs["fig8"]
    assert f8["clip"][3] - f8["clip"][1] > 250 and f8["confidence"] == "high"


def test_rsc_label_and_text_on_same_row():
    """RSC: "Fig. 2" の札と本文が同じ行の別の塊でも、9 図すべてにキャプションの本文が付く。"""
    doc, figs = _figs("rsc")
    assert {f"fig{i}" for i in range(1, 10)} <= set(figs)
    for i in range(1, 10):
        assert len(figs[f"fig{i}"]["caption"]) > 30, figs[f"fig{i}"]["caption"]


def test_acs_running_header_and_continued_table():
    """ACS（818pt）: 柱 "Energy & Fuels" と、同じページ上部の "Table 2. continued" を図に入れない。"""
    doc, figs = _figs("acs")
    for f in figs.values():
        if f["id"].startswith("fig"):
            assert f["clip"][1] > 57.8, (f["id"], f["clip"])
    assert figs["fig3"]["clip"][1] >= 226                # 表の脚注の下端（y=226）より下から（旧: 表ごと y=78 から）



def test_wiley_side_captions():
    """Wiley（Plant J）: 右の細い段のキャプションは左に図（図1・3・4・7・8）、左の段なら右に図（図6）。
    旧: 上下を探して図3・図4 がどちらも図3の右半分になった。下の柱（© 2013 …）と右余白の縦書きも入れない。"""
    doc, figs = _figs("wiley_side")
    for i in (1, 3, 4, 7, 8):
        f = figs[f"fig{i}"]
        assert f["found"] == "left" and f["confidence"] == "high", f
        assert f["clip"][2] < f["caption_bbox"][0] and f["clip"][0] < 50, f      # 左端は本文の左端（奇数ページ 49pt）まで
        assert f["clip"][3] < 734, f                                           # "© 2013 The Authors" は y=734
    assert figs["fig3"]["clip"][3] < figs["fig4"]["clip"][1]                  # 同じページの図3と図4が重ならない
    f6 = figs["fig6"]
    assert f6["found"] == "right" and f6["clip"][2] > 545                     # 右端は本文の右端まで（対称とみなさない）


def test_rsc_masthead_not_running_header():
    """RSC: 1 ページ目の大きな誌名 "Green Chemistry" を柱とみなさない（旧: 帯が 71.8 まで広がり図の上端が切れた）。"""
    doc, figs = _figs("rsc_masthead")
    assert figs["fig4"]["clip"][1] < 50, figs["fig4"]["clip"]


def test_label_row_joined_despite_superscript():
    """RSC: 本文側の上付き "H+" で上端が 3.3pt 上がっても、札だけの行 "Fig. 6" に右の本文をつなぐ。"""
    label = _ln(42.5, 242.54, 61.7, 249.44, "Fig. 6", 7.5)
    body = _ln(69.2, 239.22, 441.5, 249.44, "Overlay of THF SEC of EtOH/H+ extracted", 7.5)
    joined, used = F._join_label_row(label, [label, body])
    assert used is body and joined["text"].startswith("Fig. 6 Overlay")
    assert joined.x1 == 441.5
    # 札の続きが無い行・下端がずれた行・候補が 2 つの行はつながない（本文や表のセルを誤ってつながない）
    assert F._join_label_row(_ln(42.5, 242.5, 120, 249.4, "Fig. 6 shows the", 7.5), [body])[1] is None
    assert F._join_label_row(label, [label, _ln(69.2, 239.2, 441.5, 246.0, "other", 7.5)])[1] is None
    twin = _ln(69.2, 242.0, 300, 249.4, "twin", 7.5)
    assert F._join_label_row(label, [label, body, twin])[1] is None


def test_rsc_superscript_caption_full_width():
    """Sapouna 2021: Fig.6/8 は全幅の図（旧: キャプションが札だけで左段と判定され、右のパネルを切り落とした）。
    Fig.9 の上端に Fig.8 のキャプション 2 行目を入れない。Jev の判定で見つけた（2026-10-31）。"""
    doc, figs = _figs("rsc_masthead")
    for fid in ("fig6", "fig8"):
        f = figs[fid]
        assert f["layout"] == "full" and f["clip"][2] > 500, (fid, f["clip"])
        assert len(f["caption"]) > 30
    assert figs["fig9"]["clip"][1] >= 247, figs["fig9"]["clip"]       # 2 行目の下端 247.51（旧 237.5）


def test_a4_bottom_captions_and_roman_tables():
    """JJAP（A4・842pt）: ページ最下段（y≈780）のキャプションも拾い、Table I・II も取る。
    旧: 余白の判定にページの高さ 0（＝792pt 扱い）を渡し、y>750 の Fig.6/8/11/13 を捨てた。表はローマ数字で拾えなかった。"""
    doc, figs = _figs("jjap_a4")
    assert {f"fig{i}" for i in range(1, 15)} | {"table1", "table2"} <= set(figs)
    assert figs["table2"]["label"] == "Table II" and figs["table1"]["page"] == 7


def test_flat_figure_not_replaced_by_next():
    """JAAP: 高さ 23pt の横長の模式図（Nomura 2025 Fig.4）を小さすぎとして捨てず、下の Fig.5 の絵も取らない。"""
    doc, figs = _figs("jaap_flat")
    f4, f5 = figs["fig4"], figs["fig5"]
    assert f4["found"] == "above" and f4["clip"][3] <= f4["caption_bbox"][1], f4
    assert f4["clip"][3] <= f5["clip"][1]                                    # Fig.4 と Fig.5 が重ならない


def test_springer_side_figures_stacked():
    """Springer: 左の細い段にキャプションが縦に 2 つ並ぶページで、上の図を下のキャプションの手前で止める
    （旧: Wang 2024 Fig.4 が下の Fig.5 までページ下まで伸びた）。"""
    doc, figs = _figs("springer_side_stack")
    assert figs["fig4"]["clip"][3] < figs["fig5"]["caption_bbox"][1], figs["fig4"]["clip"]


def test_figure_below_table_excludes_table():
    """RSC: 同じページの上に表がある図は、表の本体を抱き込まない（旧: Yao 2024 Fig.8 が Table 1、Fig.9 が Table 2 を取り込んだ）。"""
    doc, figs = _figs("rsc_fig_under_table")
    for fid, tid in (("fig8", "table1"), ("fig9", "table2")):
        assert figs[fid]["page"] == figs[tid]["page"]
        assert figs[fid]["clip"][1] >= figs[tid]["clip"][3] - 5, (fid, figs[fid]["clip"], figs[tid]["clip"])
    # 表の脚注（"nd, not detected; …" y=233〜254・"Glu, glucose; …" y=188〜207）も入れない
    assert figs["fig8"]["clip"][1] > 254 and figs["fig9"]["clip"][1] > 207

def test_springer_axes_single_lines_and_colored_right_axis():
    """Springer（Origin）: 軸が幅 0 の 1 本線のパス（pymupdf では空の矩形）で、右の縦軸が色つき。
    旧: 軸の線が丸ごと落ちて枠 0 → 曲線もピーク候補も無し。"""
    from pfe.geometry import analyze_figure
    doc, figs = _figs("springer_side")
    f2 = figs["fig2"]
    g = analyze_figure(doc[f2["page"] - 1], pymupdf.Rect(f2["clip"]))
    assert len(g["panels"]) == 1
    p = g["panels"][0]
    sides = {t["side"] for t in p["ticks"]}
    assert {"left", "bottom"} <= sides
    assert len(p["curves"]) >= 3


@pytest.mark.parametrize("key, n", [("springer_text", 117), ("springer_side", 47), ("rsc", 29)])
def test_reference_list_styles(key, n):
    """参考文献の書き方: Springer "1. Lieth H"・RSC "1 Z. Sun"（見出しが太字でない）。旧: どちらも 0 件。"""
    from pfe import cite as C, text as T
    doc, figs = _figs(key)
    refs = C.parse_references(doc, T.extract_text(doc, list(figs.values())))
    nums = [e["num"] for e in refs["entries"]]
    assert nums == list(range(1, n + 1))
    # 図の言及の近くの [n] は一覧の番号に必ず結び付く（一覧が無いと run.py が KeyError で落ちた）
    cited = {c["num"] for v in refs["by_figure"].values() for c in v}
    assert cited <= set(nums)


def test_label_value_mismatch():
    """赤丸の札の値（本文）と図の頂点が離れたら警告する。横軸の範囲外の数値（昇温速度など）は比べない。"""
    from pfe.resolve import label_value_mismatch
    cal = {"a": 1.0, "b": 0.0, "log": False}
    curve = {"profile": [[x, 0, 0, 0] for x in range(0, 801, 10)]}
    assert label_value_mismatch("350 °C（本文値）", 332.8, curve, cal)        # Kawamoto 2017 Fig.1: 17 °C 差
    assert label_value_mismatch("353 °C", 351.0, curve, cal) is None          # Taira Fig.1 の精度
    assert label_value_mismatch("20 K毎分", 610.0, {"profile": [[x, 0, 0, 0] for x in range(300, 1201, 10)]}, cal) is None
    assert label_value_mismatch("低温側の谷", 355.0, curve, cal) is None


def test_control_chars_become_spaces():
    from pfe.pdfutil import norm_chars
    assert norm_chars("2.2.4 \x07Pyrolysis kinetic analysis") == "2.2.4  Pyrolysis kinetic analysis"
