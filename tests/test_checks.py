"""捏造防止の検査の回帰テスト（Codex 対敵レビュー 2026-10-28 の指摘 1・2・5 の再発防止）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pfe.analyze import unsupported_numbers, check_analysis  # noqa: E402
from pfe.build import verify_steps  # noqa: E402
from pfe import resolve as R  # noqa: E402

SRC = {"caption": "Fig. 1. TG and DTG curves of Japanese cedar wood and MWL (heating rate: 10 ◦C/min).",
       "para:p1": "The TG and DTG curves ... with the DTG peaks at 353 ◦C and 348 ◦C, respectively."}
GEOM = {"panels": [], "regions": []}


def step(say, kind="fact", quote="with the DTG peaks at 353 ◦C and 348 ◦C", label=None):
    return {"say": say, "caption_html": say, "point": None, "kind": kind,
            "actions": [{"type": "circle_peak", "label": label}] if label else [],
            "evidence": [{"source": "para:p1", "quote": quote}] if quote else []}


def test_number_from_text_passes():
    assert unsupported_numbers(step("ピークは 353 °C と 348 °C"), SRC, set()) == []


def test_invented_number_caught_even_as_background():
    # 「一般知識」の札を付けても、本文に無い数値は通さない（指摘 2）
    st = step("ピークは 999 °C です", kind="background", quote=None)
    assert unsupported_numbers(st, SRC, set()) == ["999"]
    out = verify_steps([st], SRC, {})
    assert out[0]["kind"] == "unverified"


def test_invented_number_in_circle_label_caught():
    st = step("ピークを見ます", label="999 °C")
    assert unsupported_numbers(st, SRC, set()) == ["999"]


def test_tick_values_are_allowed():
    # 目盛りから読んだ数値（図に書かれている値）は説明に使ってよい
    assert unsupported_numbers(step("横軸は 100 から 900 °C"), SRC, {"100", "900"}) == []


def test_short_quote_rejected():
    # " " や "TG" のような短い引用は根拠と認めない（指摘 1）
    out = verify_steps([step("TG の曲線です", quote="TG")], SRC, {})
    assert out[0]["evidence"][0]["verified"] is False
    assert out[0]["kind"] == "unverified"
    a = {"panels": [], "steps": [step("TG の曲線です", quote=" ")]}
    assert any("短すぎる" in e for e in check_analysis(a, GEOM, SRC))


def test_quote_matches_across_width_and_line_breaks():
    # 原文の改行の空白・行末ハイフン・全角の数字の違いで、実在する引用を [要確認] にしない（nishikiori2015・lee1990）
    src = {"para:p1": "リグニンと多糖類がそれぞれ0.6および0.25ほどの変換率で関与 しているThis indi- cates that wood lignin also"}
    out = verify_steps([step("比率です", quote="それぞれ０．６および０．２５ほどの変換率で関与している")], src, {})
    assert out[0]["evidence"][0]["verified"] is True
    out = verify_steps([step("反応です", quote="This indi-cates that wood lignin")], src, {})
    assert out[0]["evidence"][0]["verified"] is True
    # 空白を消しても短い引用は通さない（長さは空白を詰めた形で測る）
    out = verify_steps([step("短い", quote="0 . 6 および")], src, {})
    assert out[0]["evidence"][0]["verified"] is False


def test_misread_tick_rejected_at_one_percent():
    # 200 を 210 と読み違えた（残差 1.6%）較正は通さない（指摘 5）
    with pytest.raises(R.CalibrationError):
        R.fit_axis([(0, 100), (10, 210), (20, 300), (30, 400)])


def test_figure_read_numbers_are_separated_from_fabrication():
    # 引用は本文で確かめられ、数値だけ本文に無い → 図の中の文字から読んだ値として区別する（[要確認] にしない）
    st = step("300〜320 °C の段を見ます")
    out = verify_steps([st], SRC, {})
    assert out[0]["kind"] == "figure_read"
    assert out[0]["unsupported_numbers"] == ["300", "320"]


def test_integer_trailing_zero_not_matched():
    # 本文の [35] で 350 °C が通ってはいけない（Fable 5.1 二段目レビュー）。小数の 350.0 は 350 と同じ
    src = {"para:p1": "as reported previously [35], the peak was sharp"}
    assert unsupported_numbers({"say": "ピークは 350 °C"}, src, set()) == ["350"]
    assert unsupported_numbers({"say": "350.0 °C"}, {"para:p1": "at 350 °C"}, set()) == []


def test_comma_thousands():
    src = {"para:p1": "heated up to 1,000 °C"}
    assert unsupported_numbers({"say": "1,000 °C まで"}, src, set()) == []
    assert unsupported_numbers({"say": "5,000 °C まで"}, src, set()) == ["5000"]


def test_api_answer_with_extra_object():
    # API の答えの後ろに 2 つ目の JSON が付いても、steps を持つ方を使う（Sapouna Fig.10・2026-10-31）
    from pfe.analyze import _first_answer_object
    ans = {"fig_type": "bar", "summary_ja": "x", "panels": [], "steps": [], "takeaways_ja": []}
    import json
    assert _first_answer_object('{"note": "a"}\n\n' + json.dumps(ans)) == ans
    assert _first_answer_object(json.dumps(ans) + '\n{"extra": 1}') == ans
    assert _first_answer_object('{"note": "a"} 説明') is None


def test_reading_order_mixes_kinds_by_page_and_keeps_numbering():
    """図・表をページの若い順に。種類ごとの番号の順と「続き」は崩さない（smith2016 の表 1 の続き）。"""
    from pfe.build import reading_order
    figs = [
        {"id": "fig1", "page": 2, "clip": [0, 300, 0, 0], "layout": "left"},
        {"id": "fig2", "page": 5, "clip": [0, 60, 0, 0], "layout": "full"},
        {"id": "table1", "page": 4, "clip": [0, 700, 0, 0], "layout": "left"},
        {"id": "table1-cont", "page": 4, "clip": [0, 60, 0, 0], "layout": "left"},
        {"id": "table2", "page": 2, "clip": [0, 100, 0, 0], "layout": "right"},
        {"id": "tableS1", "page": 1, "clip": [0, 50, 0, 0], "layout": "full"},
    ]
    assert [f["id"] for f in reading_order(figs)] == ["tableS1", "fig1", "table1", "table1-cont", "table2", "fig2"]
