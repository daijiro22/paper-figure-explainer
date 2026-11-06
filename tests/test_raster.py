"""画像の図から部品の候補を切り出す（pfe/raster.py）のテスト。合成した画像で確かめる。"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pfe.raster import add_raster_regions, raster_boxes  # noqa: E402


def _fig(path: Path) -> Path:
    """白地に: 長い軸線 2 本・黄と青の棒・字の代わりの小さな黒い四角が並んだ「語」を 1 つ。"""
    im = Image.new("RGB", (1200, 800), "white")
    d = ImageDraw.Draw(im)
    d.line([100, 700, 1150, 700], fill="black", width=4)          # 横軸
    d.line([100, 50, 100, 700], fill="black", width=4)            # 縦軸
    d.rectangle([300, 300, 400, 698], fill=(255, 255, 0))          # 黄の棒
    d.rectangle([450, 500, 550, 698], fill=(0, 170, 240))          # 青の棒
    for i in range(6):                                             # 字が 6 つ並んだ語（隙間 6px）
        d.rectangle([700 + i * 26, 200, 720 + i * 26, 228], fill="black")
    im.save(path)
    return path


def _inside(a, b, tol=6):
    """a が b にほぼ一致するか（誤差 tol 画素）。"""
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_bars_and_words_found_axes_dropped(tmp_path):
    boxes = raster_boxes(str(_fig(tmp_path / "f.png")))
    assert any(_inside(b, [300, 300, 401, 699]) for b in boxes)      # 黄の棒
    assert any(_inside(b, [450, 500, 551, 699]) for b in boxes)      # 青の棒
    assert any(_inside(b, [700, 200, 851, 229]) for b in boxes)      # 6 字が 1 つの語にまとまる
    assert not any(b[2] - b[0] > 900 or b[3] - b[1] > 600 for b in boxes)   # 軸線は候補にしない


def test_box_outside_zoom_is_flagged():
    """寄る範囲（zoom_label）の外を囲む（box_label）と検査で落ちる。中なら通る。"""
    from pfe.analyze import check_analysis

    geom = {"panels": [], "regions": [{"id": "G1", "bbox": [0, 0, 100, 100]}, {"id": "G2", "bbox": [10, 10, 30, 30]},
                                       {"id": "G3", "bbox": [500, 500, 520, 520]}]}

    def ana(box):
        act = {k: None for k in ("panel", "side", "curve", "peak", "x_range", "y_range", "axis", "extremum", "label",
                                 "box", "from", "to")}
        return {"panels": [], "steps": [{"say": "ここ", "actions": [dict(act, type="zoom_label", labels=["G1"]),
                                                                  dict(act, type="box_label", labels=[box])],
                                         "evidence": []}]}
    assert not check_analysis(ana("G2"), geom, {"caption": "x"})
    assert any("寄る範囲の外" in e for e in check_analysis(ana("G3"), geom, {"caption": "x"}))


def test_regions_keep_existing_ids_and_convert_to_pt(tmp_path):
    png = _fig(tmp_path / "f.png")
    geom = {"clip": [50.0, 100.0, 650.0, 500.0], "scale": 2.0, "kind": "raster",
            "regions": [{"id": "G1", "bbox": [50, 100, 650, 500]}]}
    n = add_raster_regions(geom, str(png))
    assert n >= 3 and geom["regions"][0]["id"] == "G1" and geom["regions"][1]["id"] == "G2"
    yellow = [g for g in geom["regions"] if g.get("src") == "raster" and _inside(g["bbox"], [200, 250, 250.5, 449.5], 3)]
    assert yellow, "画素 → PDF 座標の換算（clip の原点 + 画素 / scale）"
    # 2 回目は前の候補を入れ替える（増え続けない）
    assert add_raster_regions(geom, str(png)) == n and len(geom["regions"]) == n + 1
