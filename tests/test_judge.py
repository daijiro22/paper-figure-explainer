"""切り出しの判定（pfe/judge.py）のテスト。Jev は呼ばない（httpx.post を差し替える）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pfe import judge  # noqa: E402

FIG = {"id": "fig2", "label": "Fig. 2", "kind": "figure", "page": 1, "clip": [100.0, 100.0, 300.0, 250.0],
       "caption": "Fig. 2. TG and DTG curves."}


def _within_limits(img: Image.Image) -> bool:
    w, h = img.size
    return 280 <= w <= 1568 and 280 <= h <= 1568 and w * h <= 2_458_624


@pytest.mark.parametrize("size", [(1800, 1200), (200, 150), (2000, 100), (1568, 1568), (300, 2400)])
def test_prepare_image_fits_limits(size):
    """大きい図（長辺 1800 の fig.png）も、小さい図も、極端に横長の図も制限内に収まる。"""
    out = judge.prepare_image(Image.new("RGB", size, "gray"))
    assert _within_limits(out)
    assert max(out.size) == 1568


def test_closeup_rect_stays_in_page():
    r = judge.closeup_rect([5.0, 10.0, 595.0, 200.0], 600, 800)
    assert r[0] == 0.0 and r[1] == 0.0 and r[2] == 600
    # 小さい図でも最低 18 pt は外側を見せる（10 pt 削れた例が見えるように）
    r = judge.closeup_rect([100.0, 100.0, 140.0, 120.0], 600, 800)
    assert r[1] <= 100 - 18 and r[3] >= 120 + 18


def test_build_request_refs_and_kind():
    refs = judge.new_refs()
    body = judge.build_request(dict(FIG, kind="table"), refs)
    assert body["model"] == judge.MODEL and body["model"] != "jev-latest"
    # 画像の参照はどれも state に現れ、assets の part は multipart の部品名
    for k, ref in refs.items():
        assert body["state"][k] == ref
        assert body["assets"][ref] == {"type": "image", "part": k}
    text = json.dumps(body["questions"])
    assert "table" in text and "{kind}" not in text
    assert set(body["questions"]) == set(judge.QUESTIONS)


def _resp(**p):
    ans = {q: {"type": "noul", "noul": v} for q, v in p.items()}
    ans["where"] = {"type": "box", "box": {"left": 100 / 600, "top": 100 / 800, "right": 300 / 600, "bottom": 250 / 800},
                    "confidence": 0.9}
    return {"model": "jev-1.14.0", "answers": ans, "usage": {"input_tokens": 1234}}


def test_to_verdict_ok_check_ng():
    good = dict(right_target=0.95, cut_off=0.1, missing_part=0.1, stray_text=0.2, caption_inside=0.05)
    v = judge.to_verdict(_resp(**good), FIG, (600, 800))
    assert v["verdict"] == "ok" and v["flags"] == []
    assert v["box_iou"] == pytest.approx(1.0)

    t = judge.THRESHOLDS["cut_off"]
    v = judge.to_verdict(_resp(**dict(good, cut_off=t)), FIG, (600, 800))     # しきい値ちょうどで旗
    assert v["verdict"] == "check" and v["flags"] == ["cut_off"]

    v = judge.to_verdict(_resp(**dict(good, right_target=0.2, stray_text=0.9)), FIG)
    assert v["verdict"] == "ng" and v["flags"][0] == "wrong_target" and "stray_text" in v["flags"]
    assert v["box_iou"] is None


def test_to_verdict_missing_answer_is_error():
    with pytest.raises(judge.JudgeError) as e:
        judge.to_verdict(_resp(right_target=0.9, cut_off=0.1), FIG)
    assert e.value.kind == "format"


class _R:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_call_jev_multipart_and_retry(monkeypatch):
    """依頼は request 部品＋画像部品の multipart。画像の見出しを付け、429 は待ってやり直す。"""
    httpx = pytest.importorskip("httpx")
    calls = []
    replies = [_R(429, headers={"retry-after": "0"}), _R(200, {"answers": {}})]

    def fake_post(url, headers, files, timeout):
        calls.append((url, headers, files))
        return replies.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    body = judge.build_request(FIG, {"page": "img-a", "closeup": "img-b", "crop": "img-c"})
    out = judge.call_jev(body, {"page": b"P", "closeup": b"C", "crop": b"X"}, "k")
    assert out == {"answers": {}} and len(calls) == 2
    url, headers, files = calls[0]
    assert url == judge.ENDPOINT
    assert headers["TypeSafe-Danger-Zone-Images"] == "true" and headers["Authorization"] == "Bearer k"
    names = [n for n, _ in files]
    assert names[0] == "request" and set(names[1:]) == set(judge.IMAGES)
    assert json.loads(files[0][1][1])["model"] == judge.MODEL


def test_call_jev_auth_error(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(401, {"error": "bad key"}))
    with pytest.raises(judge.JudgeError) as e:
        judge.call_jev({}, {}, "k")
    assert e.value.kind == "auth"


def test_api_key_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    import pfe.analyze as an
    monkeypatch.setattr(an, "ENV_FILE", tmp_path / "none")
    monkeypatch.setattr(an.load_env_file, "__defaults__", (tmp_path / "none",))
    with pytest.raises(judge.JudgeError) as e:
        judge.api_key()
    assert e.value.kind == "no_key"


def test_make_images_draws_box_outside_clip():
    """合成 PDF で、赤枠が clip の外側に描かれ、clip の内側（図の端）は塗りつぶさない。"""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.draw_rect(pymupdf.Rect(*FIG["clip"]), color=(0, 0, 1), fill=(0, 0, 1))
    imgs = judge.make_images(doc, FIG)
    assert set(imgs) == set(judge.IMAGES)
    assert all(_within_limits(im) for im in imgs.values())
    assert imgs["crop"].getpixel((5, 5))[2] > 200                        # crop は枠の中身だけ（青）
    full = imgs["page"]
    z = full.height / 800
    x0, y0 = FIG["clip"][0] * z, FIG["clip"][1] * z
    assert full.getpixel((int(x0 - 2), int(y0 + 50)))[0] > 200          # 外側は赤
    r, g, b = full.getpixel((int(x0 + 2), int(y0 + 50)))
    assert b > 200 and r < 50                                            # 内側は図（青）のまま
