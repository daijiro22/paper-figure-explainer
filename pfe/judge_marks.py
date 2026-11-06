"""解説の各ステップの注記（四角・赤丸・帯・矢印・軸・曲線のなぞり）が、意味のある所を指しているかを Jev に判定させる。

施主 2026-10-31「意味不明なところを囲っていたりするから」。切り出しの判定（pfe/judge.py）と同じ Jev・同じ版を使う。
bundle.json の shapes は図の画像（fig/<id>.png）の画素の座標なので、player を撮らずに図へ描き直して見せる。
注記は 1 色（マゼンタ）で描き、M1, M2 … の番号札を付けて、注記ごとに 2 問聞く:
    M{i}_empty    : 何も無い所・文字の切れ端など、意味の無い所を囲っているか（Yes が悪い）
    M{i}_relevant : ナレーション（日本語）と札が言っている物の上にあるか（No が悪い）
寄り（zoom）は注記ではないので描かない。判定は bundle を書き換えない（直すのは人）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import judge

MARK_COLOR = (255, 0, 200)     # 図の中にまず無い色（player の青・赤・水色と見分けがつくように）
MAX_MARKS = 6                  # 1 ステップで聞く注記の数の上限（それ以上は数えるだけ）
BOX_PAD = 16                   # player と同じく四角は箱より一回り外に引く（図の長辺 1800 px のときの画素）

QUESTIONS = {
    "empty": (
        "In `screen`, the magenta mark tagged M{i} was drawn on the figure by an annotation tool. "
        "Does M{i} enclose or point at nothing meaningful: empty white space, a fragment of a word or of a "
        "line, or a random spot? Answer No if it covers a whole element of the {kind}, such as a curve, a peak, "
        "an axis with its tick labels, a legend entry, a text label, a panel, a chemical structure or a table "
        "cell or row."
    ),
    "relevant": (
        "`narration` is Japanese speech that is heard while `screen` is shown, and `marks.M{i}.label` is the "
        "short tag shown with mark M{i} (it may be empty). Is the magenta mark M{i} placed on the thing that the "
        "narration talks about?"
    ),
}
# 2026-10-31 に Taira・Sapouna の 214 ステップで目視: 本当におかしい注記は relevant 0.05〜0.13・empty 0.63〜0.74、
# まともな枠でも empty 0.5〜0.67 が出た → 旗は empty 0.65 以上・relevant 0.3 未満（例が少なく仮）
THRESHOLDS = {"empty": 0.65}       # これ以上で旗
RELEVANT_MIN = 0.3                 # これ未満で旗
FLAG_JA = {"empty": "意味の無い所を囲っている", "off_topic": "ナレーションと違う所を指している"}


def marks_of(step: dict) -> list[dict]:
    """ステップの注記（寄りを除く）。"""
    return [s for s in step.get("shapes") or [] if s.get("kind") != "zoom"]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow 10.1 より前
        return ImageFont.load_default()


def draw_marks(fig_png: Path, marks: list[dict]) -> Image.Image:
    """図の画像に注記を描き、M1… の札を付ける。線の太さは図の長辺に比例させる。"""
    img = Image.open(fig_png).convert("RGB")
    k = max(img.size) / 1800
    lw, pad = max(3, round(7 * k)), BOX_PAD * k
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    tags = []
    for i, s in enumerate(marks[:MAX_MARKS], 1):
        kind = s.get("kind")
        if kind in ("box", "axis", "band"):
            x0, y0, x1, y1 = s["box"]
            x0, x1 = sorted((x0, x1))      # 左右・上下が逆の箱がある（--all で落ちた）
            y0, y1 = sorted((y0, y1))
            if kind == "box":
                x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
            if kind == "band":
                d.rectangle([x0, y0, x1, y1], fill=MARK_COLOR + (45,))
            d.rectangle([x0, y0, x1, y1], outline=MARK_COLOR + (255,), width=lw)
            if kind == "axis" and s.get("line"):
                d.line([tuple(p) for p in s["line"]], fill=MARK_COLOR + (255,), width=lw)
            tags.append((x0, y0))
        elif kind == "circle":
            (cx, cy), r = s["center"], abs(s["r"])
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=MARK_COLOR + (255,), width=lw)
            tags.append((cx - r, cy - r))
        elif kind == "arrow":
            (fx, fy), (tx, ty) = s["from"], s["to"]
            d.line([(fx, fy), (tx, ty)], fill=MARK_COLOR + (255,), width=lw)
            L = max(1.0, ((tx - fx) ** 2 + (ty - fy) ** 2) ** 0.5)
            ux, uy, hs = (tx - fx) / L, (ty - fy) / L, 30 * k
            d.polygon([(tx, ty), (tx - ux * hs - uy * hs * .6, ty - uy * hs + ux * hs * .6),
                       (tx - ux * hs + uy * hs * .6, ty - uy * hs - ux * hs * .6)], fill=MARK_COLOR + (255,))
            tags.append((fx, fy))
        elif kind == "trace":
            pts = [tuple(p) for p in s.get("points") or []]
            if len(pts) >= 2:
                d.line(pts, fill=MARK_COLOR + (255,), width=lw, joint="curve")
                tags.append(pts[len(pts) // 2])     # 始点は複数の曲線で重なりやすいので中ほどに札
            else:
                tags.append(None)
        else:
            tags.append(None)
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    # 番号札: 注記の左上の外側に、白抜きの字で
    dt = ImageDraw.Draw(img)
    f = _font(max(20, round(34 * k)))
    for i, t in enumerate(tags, 1):
        if t is None:
            continue
        x, y = t
        tx, ty = max(0, x - 6 * k), max(0, y - 46 * k)
        bb = dt.textbbox((tx, ty), f"M{i}", font=f)
        dt.rectangle([bb[0] - 4, bb[1] - 3, bb[2] + 4, bb[3] + 3], fill=MARK_COLOR)
        dt.text((tx, ty), f"M{i}", font=f, fill="white")
    return img


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def build_request(fig: dict, step: dict, ref: str) -> dict:
    """1 ステップ分の依頼。注記ごとに empty と relevant を聞く。"""
    marks = marks_of(step)[:MAX_MARKS]
    kind = "table" if str(fig.get("id", "")).startswith("table") else "figure"
    questions = {}
    for i, _ in enumerate(marks, 1):
        for q, text in QUESTIONS.items():
            questions[f"M{i}_{q}"] = {"type": "noul", "instructions": text.format(i=i, kind=kind)}
    return {
        "model": judge.MODEL,
        "state": {
            "figure_caption": (fig.get("caption") or "")[:600],
            "narration": step.get("say") or "",
            "subtitle": _plain(step.get("caption_html")),
            "marks": {f"M{i}": {"type": s.get("kind"), "label": s.get("label") or ""}
                      for i, s in enumerate(marks, 1)},
            "screen": ref,
        },
        "assets": {ref: {"type": "image", "part": "screen"}},
        "questions": questions,
    }


def signature(fig: dict, step: dict) -> str:
    src = json.dumps({"model": judge.MODEL, "q": QUESTIONS, "say": step.get("say"), "label": step.get("caption_html"),
                      "shapes": marks_of(step), "fig": fig.get("id"), "v": [MARK_COLOR, BOX_PAD, MAX_MARKS]},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def to_verdict(resp: dict, n_marks: int) -> dict:
    """{flags: ["M2:empty", ...], p: {質問: 確率}, verdict: ok|check}。"""
    p = {}
    for qid, a in (resp.get("answers") or {}).items():
        if a.get("type") == "noul" and isinstance(a.get("noul"), (int, float)):
            p[qid] = round(float(a["noul"]), 3)
    flags, severity = [], 0.0
    for i in range(1, min(n_marks, MAX_MARKS) + 1):
        e, r = p.get(f"M{i}_empty"), p.get(f"M{i}_relevant")
        if e is None or r is None:
            raise judge.JudgeError("format", f"M{i} の答えが無い")
        if e >= THRESHOLDS["empty"]:
            flags.append(f"M{i}:empty")
        if r < RELEVANT_MIN:
            flags.append(f"M{i}:off_topic")
        severity = max(severity, e, 1 - r)    # 目で見る順（ひどい順）に並べるための点数
    return {"verdict": "check" if flags else "ok", "flags": flags, "p": p, "severity": round(severity, 3),
            "model": resp.get("model"), "input_tokens": (resp.get("usage") or {}).get("input_tokens")}


def reflag(r: dict) -> dict:
    """保存した確率から、今のしきい値で旗を立て直す（しきい値を変えても Jev を呼び直さない）。"""
    if not r.get("p"):
        return r
    n = max(int(k[1:].split("_")[0]) for k in r["p"] if k.startswith("M"))
    return {**r, **{k: v for k, v in to_verdict({"answers": {q: {"type": "noul", "noul": x} for q, x in r["p"].items()},
                                                  "model": r.get("model")}, n).items()
                    if k in ("verdict", "flags", "severity")}}


def judge_step(work: Path, fig: dict, step: dict, key: str | None, save: Path | None = None) -> dict | None:
    """1 ステップを判定する。注記が無ければ None。key が None なら画像と依頼を書くだけ（dry-run）。"""
    marks = marks_of(step)
    if not marks:
        return None
    img = judge.prepare_image(draw_marks(work / "fig" / f"{fig['id']}.png", marks))
    ref = f"img-{uuid.uuid4().hex}"
    body = build_request(fig, step, ref)
    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        img.save(save)
    if key is None:
        return {"dry_run": True, "n_marks": len(marks)}
    resp = judge.call_jev(body, {"screen": judge.png_bytes(img)}, key)
    out = to_verdict(resp, len(marks))
    out.update(sig=signature(fig, step), n_marks=len(marks), at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return out
