"""注記の判定（pfe/judge_marks.py）のテスト。Jev は呼ばない。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pfe import judge, judge_marks as JM  # noqa: E402

STEP = {"say": "横軸は温度です。", "caption_html": "横軸は<em>温度</em>",
        "shapes": [{"kind": "zoom", "box": [0, 0, 100, 100]},
                   {"kind": "box", "box": [100, 100, 300, 200], "label": "353 °C"},
                   {"kind": "circle", "center": [600, 400], "r": 40, "label": None}]}
FIG = {"id": "fig1", "caption": "Fig. 1. TG and DTG curves."}


def test_marks_skip_zoom():
    assert [m["kind"] for m in JM.marks_of(STEP)] == ["box", "circle"]
    assert JM.marks_of({"shapes": [{"kind": "zoom", "box": [0, 0, 1, 1]}]}) == []


def test_draw_marks_outside_box(tmp_path):
    """四角は箱より一回り外に描く（箱の中の図を塗らない）。札 M1 が付く。"""
    png = tmp_path / "fig1.png"
    Image.new("RGB", (1800, 1000), "white").save(png)
    im = JM.draw_marks(png, JM.marks_of(STEP))
    assert im.getpixel((200, 150)) == (255, 255, 255)              # 箱の中は白のまま
    assert im.getpixel((100 - JM.BOX_PAD, 150))[1] < 50             # 外側の線はマゼンタ
    assert im.getpixel((600 + 40, 400))[1] < 50                     # 円周


def test_build_request_per_mark_questions():
    body = JM.build_request(FIG, STEP, "img-x")
    assert set(body["questions"]) == {"M1_empty", "M1_relevant", "M2_empty", "M2_relevant"}
    assert body["state"]["marks"]["M1"] == {"type": "box", "label": "353 °C"}
    assert body["state"]["subtitle"] == "横軸は温度"
    assert body["assets"] == {"img-x": {"type": "image", "part": "screen"}}
    assert body["model"] == judge.MODEL


def _resp(**p):
    return {"model": judge.MODEL, "answers": {k: {"type": "noul", "noul": v} for k, v in p.items()}}


def test_to_verdict_and_reflag():
    v = JM.to_verdict(_resp(M1_empty=0.1, M1_relevant=0.9, M2_empty=0.7, M2_relevant=0.2), 2)
    assert v["flags"] == ["M2:empty", "M2:off_topic"] and v["verdict"] == "check"
    assert v["severity"] == pytest.approx(0.8)
    ok = JM.to_verdict(_resp(M1_empty=0.6, M1_relevant=0.35), 1)          # しきい値の内側
    assert ok["verdict"] == "ok"
    # しきい値を変えたら、保存した確率から旗を立て直せる
    old = dict(ok, flags=["M1:empty"], verdict="check")
    assert JM.reflag(old)["flags"] == []
    with pytest.raises(judge.JudgeError):
        JM.to_verdict(_resp(M1_empty=0.1), 1)
