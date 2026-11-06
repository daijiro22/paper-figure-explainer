"""図・表の切り出し（figures.json の clip）が正しく取れているかを、Jev（TypeSafe の判断モデル）に判定させる。

施主 2026-10-31「図表をちゃんと取れてるかを確かめるために Jev を入れたい」。
Jev は文章を作らず「Yes の確率」「どれか」「どこか」だけを返すモデル（https://docs.typesafe.ai/）。

1 図につき 1 回、画像 3 枚と質問 5 つ（＋Box）を送る:
    page    : ページ全体に clip を赤枠で描いた画像（離れた位置のパネルの取りこぼしを見る）
    closeup : clip の周りを少し広めに切り、同じ赤枠を描いた画像（端の数 pt の欠けを見る）
    crop    : clip の中身だけ（本文・柱・キャプションの混入を見る。枠の外の本文に引きずられないように）
質問は英語（Jev は英語がいちばん正確）。Yes が「悪い」になる向きにそろえ、しきい値で旗を立てる。
Box の答え（Jev が思う図の範囲）は clip との重なり（IoU）として記録するだけで、判定には使わない
（Box は試験段階で「無い」と答えられない・docs 2026-10-31 時点）。

ここは figures.json を読むだけで書き換えない（直すのは人。taira2026_jaap は作り直してはいけない）。
鍵は環境変数 TYPESAFE_API_KEY か ~/.config/pfe/env（施主が書く。コードに書かない・表示しない）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# 別名 jev-latest は版が上がると答えが変わる。しきい値をこの版で合わせるので版を固定する（docs の推奨）
MODEL = "jev-1.14.0"
IMAGE_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}   # 画像と Box は既定で切ってある

# 画像の制限（docs/images: 各辺 280〜1568 px・総画素 2,458,624 以下・PNG か JPEG）
MIN_SIDE, MAX_SIDE = 280, 1568
CLOSEUP_MARGIN = 0.15      # closeup は clip の幅・高さの 15% ずつ外側まで見せる
CLOSEUP_MARGIN_MIN = 18.0  # ただし最低 18 pt（ACS の柱で 10 pt 削れた例を見えるようにする）
BOX_WIDTH = 3              # 赤枠の線の太さ（px）。clip の外側に描き、図の端を隠さない
IMAGES = ("page", "closeup", "crop")   # 送る画像（state の名前＝multipart の部品名）

# ---- 質問としきい値（人が見直すのはここだけでよいように 1 か所にまとめる） ----
# どれも Noul（Yes の確率）。right_target 以外は Yes が「悪い」。
QUESTIONS = {
    "right_target": {
        "type": "noul",
        "instructions": (
            "Is the content inside the red box in `closeup` the {kind} described by `caption` "
            "(the {kind} labelled `label`), rather than body text or a different {kind}?"
        ),
    },
    "cut_off": {
        "type": "noul",
        "instructions": (
            "Does the red box in `closeup` cut through part of the {kind}, so that some of it lies outside "
            "the box? Count any panel, photo, plotted curve, axis line, tick label, axis title, legend, "
            "panel letter, table row or column, or table footnote that is cut by an edge of the box."
        ),
    },
    "missing_part": {
        "type": "noul",
        "instructions": (
            "Looking at the whole `page`, is there another part of this same {kind} (for example another "
            "panel such as (b) or (c), or the rest of a graph) that is outside the red box?"
        ),
    },
    # 混入の 2 問は切り出した中身（crop）だけに聞く。closeup に聞くと赤枠の外の本文にも Yes と答えた
    # （2026-10-31 の初回 47 図で 37 図に旗。Taira Fig.2・Fig.11 など中身は図だけのものも 0.9 超）
    "stray_text": {
        "type": "noul",
        "instructions": (
            "`crop` is an image cut out of a journal page and should show only the {kind}. Does `crop` "
            "also contain something that is not part of the {kind}: lines of body text from the article, "
            "a running header or footer, a page number, or a vertical margin notice such as "
            "'Downloaded from ...'? Labels, legends and text drawn inside the {kind} do not count."
        ),
    },
    "caption_inside": {
        "type": "noul",
        "instructions": (
            "Does `crop` contain the caption sentence itself (the text that begins with `label` and is "
            "given in `caption`)? Text that is drawn inside the {kind} does not count."
        ),
    },
    "where": {
        "type": "box",
        "instructions": "the whole {kind} described by `caption` in `page`, without its caption",
    },
}
# 旗を立てる確率。既知の悪い例（scripts/judge_figs.py の KNOWN）で合わせる。
# cut_off: 本当に欠けていた Sapouna Fig.6/8 が 0.74/0.96、欠けていない Taira Fig.8 が 0.53 → 0.6（2026-10-31・例が少なく仮）
THRESHOLDS = {"cut_off": 0.6, "missing_part": 0.5, "stray_text": 0.5, "caption_inside": 0.5}
RIGHT_TARGET_MIN = 0.5     # これ未満なら「別のものを切り出している」= ng

FLAG_JA = {"cut_off": "端が欠けている", "missing_part": "図の一部を取りこぼし", "stray_text": "本文・柱などが混入",
           "caption_inside": "キャプションが入っている", "wrong_target": "別のものを切り出している"}


class JudgeError(Exception):
    """失敗の種類（no_key / auth / rate / bad_request / server / network / format）を持つ。"""

    def __init__(self, kind: str, msg: str):
        super().__init__(f"[{kind}] {msg}")
        self.kind = kind


# ---- 画像 ----

def prepare_image(img: Image.Image) -> Image.Image:
    """Jev の制限に収める。長辺を 1568 px にそろえ（拡大も縮小も）、短辺が 280 px に満たなければ白で足す。
    長辺 1568 なら総画素は必ず上限（1568×1568）以下。"""
    img = img.convert("RGB")
    w, h = img.size
    s = MAX_SIDE / max(w, h)
    if s != 1:
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    w, h = img.size
    if min(w, h) < MIN_SIDE:
        pad = Image.new("RGB", (max(w, MIN_SIDE), max(h, MIN_SIDE)), "white")
        pad.paste(img, ((pad.width - w) // 2, (pad.height - h) // 2))
        img = pad
    return img


def _render(page, rect, long_edge: int) -> tuple[Image.Image, float]:
    """PDF のページの rect（pt）を長辺 long_edge px で描き、(画像, px/pt) を返す。読者の注釈は描かない。"""
    import pymupdf
    zoom = long_edge / max(rect.width, rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False, annots=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), zoom


def _draw_clip(img: Image.Image, clip_px) -> Image.Image:
    """clip（px）の外側に赤枠を描く。枠が図の端の画素を隠さないよう、線は clip の 1 px 外から外向きに。"""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = clip_px
    for i in range(1, BOX_WIDTH + 1):
        d.rectangle([x0 - i, y0 - i, x1 + i, y1 + i], outline=(230, 0, 0))
    return img


def closeup_rect(clip, page_w: float, page_h: float) -> list[float]:
    """clip の周りに余白を足した範囲（pt）。ページからははみ出さない。"""
    x0, y0, x1, y1 = clip
    mx = max((x1 - x0) * CLOSEUP_MARGIN, CLOSEUP_MARGIN_MIN)
    my = max((y1 - y0) * CLOSEUP_MARGIN, CLOSEUP_MARGIN_MIN)
    return [max(0.0, x0 - mx), max(0.0, y0 - my), min(page_w, x1 + mx), min(page_h, y1 + my)]


def make_images(doc, fig: dict) -> dict[str, Image.Image]:
    """{"page": ページ全体＋赤枠, "closeup": clip の周り＋赤枠}（どちらも制限内に収めたもの）。"""
    import pymupdf
    from pfe.orient import figure_view
    page, clip = figure_view(doc, fig)         # 向きの違う図表は 3 枚とも正立させて見せる（文字が読める向きで判定する）
    pr = page.rect
    x0, y0, x1, y1 = clip

    full, z = _render(page, pr, MAX_SIDE)
    _draw_clip(full, [(x0 - pr.x0) * z, (y0 - pr.y0) * z, (x1 - pr.x0) * z, (y1 - pr.y0) * z])

    cr = closeup_rect(list(clip), pr.width, pr.height)
    near, z = _render(page, pymupdf.Rect(*cr), MAX_SIDE)
    _draw_clip(near, [(x0 - cr[0]) * z, (y0 - cr[1]) * z, (x1 - cr[0]) * z, (y1 - cr[1]) * z])

    crop, _ = _render(page, clip, MAX_SIDE)    # 赤枠の中身だけ（混入の判定用）
    return {"page": prepare_image(full), "closeup": prepare_image(near), "crop": prepare_image(crop)}


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---- 依頼 ----

def build_request(fig: dict, refs: dict[str, str]) -> dict:
    """System One の依頼本体（JSON 部分）。refs は {"page": 参照文字列, "closeup": 参照文字列}。
    画像そのものは multipart の別の部品で送り、assets の part でつなぐ。"""
    kind = "table" if fig.get("kind") == "table" else "figure"
    questions = {}
    for qid, q in QUESTIONS.items():
        q = dict(q)
        q["instructions"] = q["instructions"].format(kind=kind)
        questions[qid] = q
    return {
        "model": MODEL,
        "state": {
            "label": fig.get("label") or fig["id"],
            "caption": (fig.get("caption") or "")[:800],
            **{k: refs[k] for k in IMAGES},
        },
        "assets": {refs[k]: {"type": "image", "part": k} for k in IMAGES},
        "questions": questions,
    }


def new_refs() -> dict[str, str]:
    """画像の参照文字列。本文に偶然現れない長さにする（docs の推奨は UUID）。"""
    return {k: f"img-{uuid.uuid4().hex}" for k in IMAGES}


def signature(fig: dict) -> str:
    """同じ clip・同じ質問・同じ版なら判定をやり直さない（費用より、答えの揺れで表が変わるのを避けるため）。"""
    item = {"model": MODEL, "q": QUESTIONS, "page": fig.get("page"), "clip": fig.get("clip"),
            "caption": fig.get("caption"), "m": [CLOSEUP_MARGIN, CLOSEUP_MARGIN_MIN, BOX_WIDTH]}
    if fig.get("rotate"):
        item["rotate"] = fig["rotate"]      # 向きが無い図の署名は今までと同じ（判定をやり直さない）
    src = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def api_key() -> str:
    from pfe.analyze import load_env_file
    load_env_file()
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JudgeError("no_key", "TYPESAFE_API_KEY が無い（環境変数か ~/.config/pfe/env に施主が書く）")
    return key


def call_jev(body: dict, images: dict[str, bytes], key: str, timeout: float = 120, retries: int = 3) -> dict:
    """multipart で送る（docs の推奨。base64 より 3 割小さい）。429 と 5xx は待って最大 retries 回やり直す。"""
    import httpx
    files = [("request", (None, json.dumps(body, ensure_ascii=False), "application/json"))]
    files += [(name, (f"{name}.png", data, "image/png")) for name, data in images.items()]
    headers = {"Authorization": f"Bearer {key}", **IMAGE_HEADER}
    for attempt in range(retries + 1):
        try:
            r = httpx.post(ENDPOINT, headers=headers, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise JudgeError("network", str(e)) from e
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise JudgeError("format", r.text[:300]) from e
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
            continue
        kind = {401: "auth", 403: "auth", 429: "rate"}.get(r.status_code,
                                                            "server" if r.status_code >= 500 else "bad_request")
        raise JudgeError(kind, f"HTTP {r.status_code}: {r.text[:300]}")
    raise JudgeError("rate", "やり直しの上限")


# ---- 答えの読み方 ----

def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def to_verdict(resp: dict, fig: dict, page_size: tuple[float, float] | None = None) -> dict:
    """Jev の答えを {verdict: ok|check|ng, flags: [...], p: {質問: 確率}, box_iou} にする。
    page_size（pt）があれば Box の答え（ページ画像に対する 0〜1）を pt に直し、clip との IoU を出す。"""
    ans = resp.get("answers") or {}
    p = {}
    for qid, a in ans.items():
        if a.get("type") == "noul" and isinstance(a.get("noul"), (int, float)):
            p[qid] = round(float(a["noul"]), 3)
    missing = [q for q in THRESHOLDS if q not in p] + ([] if "right_target" in p else ["right_target"])
    if missing:
        raise JudgeError("format", f"答えに無い質問: {missing}")
    flags = [q for q, t in THRESHOLDS.items() if p[q] >= t]
    if p["right_target"] < RIGHT_TARGET_MIN:
        flags.insert(0, "wrong_target")
    verdict = "ng" if "wrong_target" in flags else ("check" if flags else "ok")

    box_iou = box_pt = None
    b = (ans.get("where") or {}).get("box")
    if page_size and isinstance(b, dict) and all(k in b for k in ("left", "top", "right", "bottom")):
        # page 画像は長辺 1568 に合わせたうえで短辺の白足しはしていない（ページは縦長で短辺 ≥ 280）
        pw, ph = page_size
        box_pt = [round(b["left"] * pw, 1), round(b["top"] * ph, 1), round(b["right"] * pw, 1), round(b["bottom"] * ph, 1)]
        box_iou = round(_iou(box_pt, fig["clip"]), 3)
    return {"verdict": verdict, "flags": flags, "p": p, "box_pt": box_pt, "box_iou": box_iou,
            "box_conf": (ans.get("where") or {}).get("confidence"), "model": resp.get("model"),
            "input_tokens": (resp.get("usage") or {}).get("input_tokens")}


def judge_figure(doc, fig: dict, key: str, save_dir: Path | None = None) -> dict:
    """1 図を判定して to_verdict の結果（＋sig）を返す。save_dir があれば送った画像と依頼を残す。"""
    imgs = make_images(doc, fig)
    refs = new_refs()
    body = build_request(fig, refs)
    data = {k: png_bytes(v) for k, v in imgs.items()}
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        for k, b in data.items():
            (save_dir / f"{fig['id']}.{k}.png").write_bytes(b)
        (save_dir / f"{fig['id']}.request.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    resp = call_jev(body, data, key)
    # Box の答えは見せた画像（正立側）に対する割合なので、正立側の枠とページの大きさで比べる
    from pfe.orient import figure_view
    vpage, vclip = figure_view(doc, fig)
    out = to_verdict(resp, dict(fig, clip=list(vclip)), (vpage.rect.width, vpage.rect.height))
    out["sig"] = signature(fig)
    out["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out
