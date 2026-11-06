"""幾何解析と較正・ピーク吸着の回帰テスト（テスト論文 Taira 2026 JAAP の Fig.1）。

正解は本文の「DTG peaks at 353 °C and 348 °C」（スギ木材・MWL の順）。図から読める精度は
線の太さで決まるので ±3 °C を合格線にする。
"""
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe.geometry import analyze_figure  # noqa: E402
from pfe import resolve as R  # noqa: E402

from _paths import local  # noqa: E402

PDF = Path(local(r"Taira et al\Taira et al. - 2026 - Pyrolysis In situ assessment of lignin pyrolysis reactions and their interactions with polysaccharides in Japanese.pdf"))
pytestmark = pytest.mark.skipif(not PDF.exists(), reason="テスト論文が無い")


@pytest.fixture(scope="module")
def geom():
    doc = pymupdf.open(PDF)
    return analyze_figure(doc[1], pymupdf.Rect(335, 605, 530, 722))


def _ids(panel, side):
    return [t["id"] for t in panel["ticks"] if t["side"] == side and t["id"]]


def test_frame_ticks_curves(geom):
    assert len(geom["panels"]) == 1
    p = geom["panels"][0]
    assert len(_ids(p, "left")) == 7      # 0〜120
    assert len(_ids(p, "right")) == 4     # 0〜3
    assert len(_ids(p, "bottom")) == 5    # 100〜900
    assert len(p["curves"]) == 4          # TG×2・DTG×2（色ではなくパスで分かれる）
    # DTG（右軸の 2 本）にだけピーク候補がある
    with_peaks = [c for c in p["curves"] if c["peaks"]]
    assert len(with_peaks) == 2


def test_calibration_and_peaks(geom):
    p = geom["panels"][0]
    bottom, right = _ids(p, "bottom"), _ids(p, "right")
    # 位置の順（左→右・上→下）に値を当てる
    xt = sorted((t for t in p["ticks"] if t["id"] in bottom), key=lambda t: t["pos"])
    yt = sorted((t for t in p["ticks"] if t["id"] in right), key=lambda t: t["pos"])
    ans = {"axes": {"x": {"ticks": {t["id"]: v for t, v in zip(xt, [100, 300, 500, 700, 900])}},
                    "y_right": {"ticks": {t["id"]: v for t, v in zip(yt, [3, 2, 1, 0])}}}}
    cals = R.calibrate_panel(p, ans)
    assert not cals["errors"]
    assert cals["x"]["resid"] < 0.005
    # 色で曲線を選ぶ: 橙 = スギ木材（Cedar）、緑 = MWL。DTG はピーク候補を持つほう
    dtg = {c["color"]: c for c in p["curves"] if c["peaks"]}
    cedar = R.find_extremum(dtg["#e87132"], cals["x"], cals["y_right"], [250, 450], "max")
    mwl = R.find_extremum(dtg["#00af50"], cals["x"], cals["y_right"], [250, 450], "max")
    assert abs(cedar["x"] - 353) <= 3
    assert abs(mwl["x"] - 348) <= 3
    # 狭い区間（K 番号経由）でも同じ答えになる
    narrow = R.find_extremum(dtg["#e87132"], cals["x"], cals["y_right"], [331, 369], "max")
    assert abs(narrow["x"] - cedar["x"]) < 1.0


def test_fit_axis_log_and_reject():
    log = R.fit_axis([(0, 1), (10, 10), (20, 100), (30, 1000)])
    assert log["log"] is True
    with pytest.raises(R.CalibrationError):
        R.fit_axis([(0, 0), (10, 10), (20, 50)])   # 直線に乗らない読み違い
