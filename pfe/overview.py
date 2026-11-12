"""論文の「概要」（再生の最初に 1 回だけ流す、アブストラクトの解説）を作る。

施主 2026-10-30「図の最初にアブストラクトの説明動画を作ってほしい。概要を把握してから読みたい」。
Codex と相談した形:
  - 画面は図の無い専用の概要画面。代表的な図を 2〜6 枚並べた「論文の地図」を 1 枚の画像にし、
    要点を話しているあいだ、その要点に対応する図の枠を光らせる（図への道案内）
  - 段階（背景・目的・方法・結果・結論）は字幕の頭に小さく付けるだけ。アブストラクトに無い段階は作らない
  - 根拠はアブストラクト（source "abstract"）。引用が実在しない・原文に無い数値があるステップは [要確認]
    （図の解説のような「図から読んだ数値」の救済はしない）
  - 図との対応は、各図の解析で検査済みの要約（summary_ja）とキャプションを見せて LLM に選ばせる
player には bundle["overview"] として渡し、player が図の列の先頭に「概要」として並べる（bundle の figures は変えない）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# 文字だけの段（概要・結論・タグ）のモデル。図を読む解析（pfe/analyze.py の PFE_API_MODEL・Opus）より安い Sonnet にする
# （施主 2026-11-10「文字の段だけ」。料金は Opus 5.5 の半分）
TEXT_MODEL = os.environ.get("PFE_TEXT_MODEL", "claude-sonnet-5-5")

SECTIONS = ["背景", "目的", "方法", "結果", "結論"]
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary_ja", "takeaways_ja", "steps"],
    "properties": {
        "summary_ja": {"type": "string"},
        "takeaways_ja": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["section", "say", "caption_html", "point", "figs", "evidence"],
            "properties": {
                "section": {"type": "string", "enum": SECTIONS},
                "say": {"type": "string"}, "caption_html": {"type": "string"},
                "point": {"type": ["string", "null"]},
                "figs": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["source", "quote"],
                    "properties": {"source": {"type": "string"}, "quote": {"type": "string"}}}},
            }}},
    },
}
MAX_MAP = 6           # 地図に並べる図の数の上限


def fig_digest(work: Path, fig_ids: list[str]) -> list[dict]:
    """各図・表の id・ラベル・キャプションの頭・解析で作った要約（検査済み）。"""
    figs = {f["id"]: f for f in json.loads((work / "figures.json").read_text(encoding="utf-8"))}
    out = []
    for fid in fig_ids:
        a = work / "fig" / f"{fid}.analysis.json"
        summ = json.loads(a.read_text(encoding="utf-8")).get("summary_ja", "") if a.exists() else ""
        f = figs.get(fid, {})
        out.append({"id": fid, "label": f.get("label") or fid, "caption": (f.get("caption") or "")[:220],
                    "summary": summ})
    return out


def build_prompt(title: str, abstract: str, digest: list[dict]) -> str:
    figs = "\n".join(f"- {d['id']}（{d['label']}）: {d['summary'] or '（要約なし）'} ／ キャプション: {d['caption']}"
                     for d in digest)
    return f"""論文の「概要」の解説（再生の最初に流す短い動画）の台本を、大学 1〜2 年生にもわかる日本語の JSON で作ってください。
この論文を初めて読む人が、図を見る前に「何を調べて、何が分かったか」と「どの図を見ればよいか」をつかむためのものです。

## 論文の題
{title}

## アブストラクト（原文。source は "abstract"）
{abstract}

## この論文の図・表（id・ラベル・その図が示すこと（検査済みの要約）・キャプションの頭）
{figs}

## 答え方
1. steps: 3〜6 コマ。アブストラクトの流れに沿って、背景・目的・方法・結果・結論のうち、アブストラクトに書かれているものだけを使う
   （無い段階は作らない。結果と結論が同じ内容ならまとめる）。
   - section: 背景 / 目的 / 方法 / 結果 / 結論 のどれか。
   - say: ナレーション 1〜3 文。です・ます調。1 文 40 字前後。専門語はかみくだく。
   - caption_html: 字幕（say を短くしたもの）。大事な語を <em>…</em> で 1〜2 個囲む。
   - point: 論文の一番大事な主張のコマだけ 12〜20 字の要点（全体で 1 回）。それ以外は null。
   - figs: そのコマの内容を示している図・表の id（上の一覧から。図の要約やキャプションに同じ内容があるものだけ。
     背景・目的など対応する図が無いコマは []。1 コマ 0〜3 個）。
   - evidence: 根拠。source は "abstract"、quote はアブストラクトから**そのまま**写した連続した一部（20〜120 字）。
     どのコマにも 1 つ以上。アブストラクトに書かれていない解釈・数値を作らない（数値はアブストラクトにあるものだけ）。
2. summary_ja: 論文全体を 1 文で。takeaways_ja: 今日のまとめ 3 項目（各 25 字以内）。

JSON だけを返してください。
"""


def _with_point(st: dict) -> dict:
    """数値の照合に POINT も入れる（図の解説の照合は say と字幕だけを見ていた。概要は要点も確かめる）。"""
    return {**st, "caption_html": f"{st.get('caption_html') or ''} {st.get('point') or ''}"}


def check(a: dict, abstract: str, fig_ids: list[str]) -> list[str]:
    """根拠の実在・原文に無い数値・図の id を検査する（空なら合格）。"""
    from pfe.analyze import match_key, quote_ok, unsupported_numbers
    src = match_key(abstract)
    errs = []
    steps = a.get("steps", [])
    if not 2 <= len(steps) <= 8:
        errs.append(f"steps は 3〜6 コマ（今 {len(steps)}）")
    for i, st in enumerate(steps):
        evs = st.get("evidence") or []
        if not evs:
            errs.append(f"steps[{i}]: evidence が無い（アブストラクトから引用する）")
        for ev in evs:
            if ev.get("source") != "abstract":
                errs.append(f"steps[{i}]: source は \"abstract\" だけ")
            elif not quote_ok(ev.get("quote", ""), src):
                errs.append(f"steps[{i}]: 引用「{ev.get('quote', '')[:40]}…」がアブストラクトに見つからない（そのまま写す）")
        bad = unsupported_numbers(_with_point(st), {"abstract": abstract}, set())
        if bad:
            errs.append(f"steps[{i}]: 数値 {', '.join(bad)} がアブストラクトに無い")
        for f in st.get("figs") or []:
            if f not in fig_ids:
                errs.append(f"steps[{i}]: figs の {f} はこの論文の図・表に無い")
    return errs


def cache_key(abstract: str, digest: list[dict]) -> str:
    return hashlib.sha1(json.dumps([abstract, digest], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def analyze_overview(work: Path, title: str, abstract: str, fig_ids: list[str], driver: str) -> dict | None:
    """台本を作る（作成済みで材料が変わっていなければ使い回す）。作れなければ None。"""
    figdir = work / "fig"
    figdir.mkdir(exist_ok=True)
    path = figdir / "overview.analysis.json"
    digest = fig_digest(work, fig_ids)
    key = cache_key(abstract, digest)
    if path.exists():
        a = json.loads(path.read_text(encoding="utf-8"))
        if a.get("key") == key or driver == "manual":
            return a
    prompt = build_prompt(title, abstract, digest)
    (figdir / "overview.prompt.md").write_text(prompt, encoding="utf-8")
    if driver != "api":
        print("  概要: API の口でないので作らない（manual は fig/overview.analysis.json を書く）")
        return None
    from pfe.analyze import DriverError, call_api
    schema_path = figdir / "overview.schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    raw = figdir / "overview.analysis.raw.json"
    a, errs = None, []
    for attempt in range(2):                  # 検査に落ちたら、指摘を添えて 1 回だけ頼み直す
        p = prompt if not errs else prompt + "\n\n## 前回の答えの問題（直して全体を出し直す）\n" + "\n".join(f"- {e}" for e in errs)
        try:
            a = call_api(p, [], schema_path, raw, model=TEXT_MODEL)
        except DriverError as e:
            print(f"  概要: API の呼び出しに失敗: {e}")
            return None
        errs = check(a, abstract, fig_ids)
        print(f"  概要: 検査の指摘 {len(errs)} 件")
        if not errs:
            break
    a["key"], a["errors"] = key, errs
    path.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
    return a


def _font(size: int):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def compose_map(work: Path, ids: list[str], labels: dict[str, str], out: Path) -> dict[str, list[float]]:
    """図を格子に並べた 1 枚の画像（論文の地図）を作り、図ごとの枠（画素）を返す。"""
    from PIL import Image, ImageDraw
    n = len(ids)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols
    W, H, gap, top = 1800, 1080, 36, 70
    cw, ch = (W - gap * (cols + 1)) / cols, (H - gap * (rows + 1)) / rows
    canvas = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(canvas)
    font = _font(40)
    boxes = {}
    for k, fid in enumerate(ids):
        r, c = divmod(k, cols)
        x0, y0 = gap + c * (cw + gap), gap + r * (ch + gap)
        with Image.open(work / "fig" / f"{fid}.png") as im:
            im = im.convert("RGB")
            s = min(cw / im.width, (ch - top) / im.height)
            im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
            ix, iy = int(x0 + (cw - im.width) / 2), int(y0 + top + (ch - top - im.height) / 2)
            canvas.paste(im, (ix, iy))
        draw.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=18, outline=(200, 206, 216), width=3)
        draw.text((x0 + 18, y0 + 14), labels.get(fid, fid), fill=(36, 87, 197), font=font)
        boxes[fid] = [round(x0, 1), round(y0, 1), round(x0 + cw, 1), round(y0 + ch, 1)]
    canvas.save(out)
    return boxes


def overview_entry(work: Path, a: dict, abstract: dict, fig_ids: list[str], no_tts: bool) -> dict:
    """bundle["overview"]（player が図の列の先頭に「概要」として並べる。図の解説と同じ形）。"""
    from pfe.analyze import match_key, quote_ok, unsupported_numbers
    figs = {f["id"]: f for f in json.loads((work / "figures.json").read_text(encoding="utf-8"))}
    labels = {fid: figs.get(fid, {}).get("label") or fid for fid in fig_ids}
    # 地図に並べる図: 台本で使った図（図の順）。足りなければ先頭の図で埋める
    used = [f for f in fig_ids if any(f in (st.get("figs") or []) for st in a["steps"])]
    ids = (used or [f for f in fig_ids if f.startswith("fig")][:4])[:MAX_MAP]
    out_png = work / "fig" / "overview.png"
    if ids:
        boxes = compose_map(work, ids, labels, out_png)
    else:                                         # 図が 1 枚も無い論文: 白い画面（字幕と声だけ）
        from PIL import Image
        Image.new("RGB", (1800, 1080), "white").save(out_png)
        boxes = {}
    src = match_key(abstract["text"])
    steps = []
    for st in a["steps"]:
        evs = [{"source": "abstract", "where": "アブストラクト", "quote": e["quote"],
                "verified": quote_ok(e["quote"], src)}
               for e in st.get("evidence", [])]
        bad = unsupported_numbers(_with_point(st), {"abstract": abstract["text"]}, set())
        kind = "fact" if any(e["verified"] for e in evs) and not bad else "unverified"
        audio, dur = None, None
        if not no_tts:
            try:
                from pfe.tts import synth
                p, dur = synth(st["say"], work / "audio")
                audio = f"audio/{p.name}"
            except Exception as e:        # 声が作れなくても再生ページは作る
                print(f"  概要: 音声を作れなかった: {e}")
        shapes = [{"kind": "box", "box": boxes[f], "label": ""} for f in (st.get("figs") or []) if f in boxes]
        steps.append({"say": st["say"], "caption_html": f"〔{st['section']}〕" + (st.get("caption_html") or st["say"]),
                      "point": st.get("point"), "kind": kind, "shapes": shapes, "warnings": [],
                      "evidence": evs, "audio": audio, "duration": dur, "unsupported_numbers": bad,
                      "section": st["section"], "figs": st.get("figs") or []})
    from PIL import Image
    with Image.open(out_png) as im:
        w, h = im.size
    return {"id": "overview", "label": "論文の概要", "caption": "", "page": abstract.get("page"),
            "img": "fig/overview.png", "width": w, "height": h, "summary_ja": a.get("summary_ja", ""),
            "takeaways_ja": a.get("takeaways_ja", []), "approx": False, "steps": steps, "refs": [],
            "calibration": {}, "map": ids, "abstract_text": abstract["text"]}
