"""論文の「結論」（全部の図・表の解説のあとに 1 回だけ流す、論文全体のまとめ）を作る。

施主 2026-11-10「すべての図の解説が終わったら結論とかまで言いたい。図を動かすってよりかは文字でいい」。
作りは概要（pfe/overview.py）と同じ:
  - 画面は、結論の根拠になった図を並べた「論文の地図」（概要と同じ部品）。話している結論に対応する図の枠を光らせる
  - 最後に「今日のまとめ」（文字のカード）。右の欄には結論の原文を出し、引用した所に蛍光ペン
  - 材料は本文の結論の節（無ければ本文の終わりの部分）と、各図の検査済みの要約
  - 根拠は本文（source "text"）。引用が本文に実在しない・本文に無い数値があるステップは [要確認]
player には bundle["conclusion"] として渡し、player が図の列の最後に「結論」として並べる。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pfe.overview import MAX_MAP, TEXT_MODEL, compose_map, fig_digest

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary_ja", "takeaways_ja", "steps"],
    "properties": {
        "summary_ja": {"type": "string"},
        "takeaways_ja": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["say", "caption_html", "point", "figs", "evidence"],
            "properties": {
                "say": {"type": "string"}, "caption_html": {"type": "string"},
                "point": {"type": ["string", "null"]},
                "figs": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["source", "quote"],
                    "properties": {"source": {"type": "string"}, "quote": {"type": "string"}}}},
            }}},
    },
}
TAIL_CHARS = 6000       # 結論の節が見つからないときに見せる本文の終わりの長さ
CONC_RE = re.compile(r"conclu|concluding|summary|結論|まとめ|おわりに", re.I)
# 本文の終わりに付く、結論ではない段落（謝辞・資金・著者の役割など）
BOILER_RE = re.compile(r"^\s*(acknowledg|funding|research funding|author contributions?|credit authorship|"
                       r"declaration|competing interests?|conflicts? of interest|data availability|"
                       r"research ethics|informed consent|supplementary|supporting information|謝辞)", re.I)


def conclusion_source(text: dict) -> tuple[str, str]:
    """結論の材料（見せる文）と、その出どころの名前。結論の節があればそれ、無ければ本文の終わり。"""
    secs = {s["id"]: s.get("title") or "" for s in text.get("sections", [])}
    paras = [p for p in text.get("paragraphs", []) if (p.get("text") or "").strip()]
    while paras and BOILER_RE.search(paras[-1]["text"]):          # 末尾の謝辞などを落とす
        paras.pop()
    conc = [p for p in paras if CONC_RE.search(secs.get(p.get("section"), ""))]
    if conc and sum(len(p["text"]) for p in conc) >= 200:
        return "\n\n".join(p["text"] for p in conc)[:TAIL_CHARS * 2], "結論の節"
    tail, n = [], 0
    for p in reversed(paras):
        if n >= TAIL_CHARS:
            break
        tail.insert(0, p["text"])
        n += len(p["text"])
    return "\n\n".join(tail), "本文の終わり"


def body_text(text: dict) -> str:
    """引用の照合に使う本文全体。"""
    return "\n".join(p.get("text") or "" for p in text.get("paragraphs", []))


def build_prompt(title: str, source: str, where: str, digest: list[dict]) -> str:
    figs = "\n".join(f"- {d['id']}（{d['label']}）: {d['summary'] or '（要約なし）'} ／ キャプション: {d['caption']}"
                     for d in digest)
    return f"""論文の「結論」の解説（全部の図・表の解説を見終わったあとに流す、短いまとめ）の台本を、大学 1〜2 年生にもわかる日本語の JSON で作ってください。
図を一つずつ見てきた人が「結局この論文で何が分かったのか」「それはどの図から言えるのか」を最後に確かめるためのものです。

## 論文の題
{title}

## 本文（{where}。source は "text"）
{source}

## この論文の図・表（id・ラベル・その図が示すこと（検査済みの要約）・キャプションの頭）
{figs}

## 答え方
1. steps: 2〜5 コマ。論文が結論として述べていることを、大事な順に。最後のコマは、この結果が何に役立つか・何が残された課題かを本文が述べていればそれを（無ければ作らない）。
   - say: ナレーション 1〜3 文。です・ます調。1 文 40 字前後。専門語はかみくだく。
   - caption_html: 字幕（say を短くしたもの）。大事な語を <em>…</em> で 1〜2 個囲む。
   - point: 論文の一番大事な結論のコマだけ 12〜20 字の要点（全体で 1 回）。それ以外は null。
   - figs: その結論の根拠になっている図・表の id（上の一覧から。図の要約やキャプションに同じ内容があるものだけ。1 コマ 0〜3 個）。
   - evidence: 根拠。source は "text"、quote は上の本文から**そのまま**写した連続した一部（20〜120 字）。
     どのコマにも 1 つ以上。本文に書かれていない解釈・数値を作らない（数値は本文にあるものだけ）。
2. summary_ja: 論文の結論を 1 文で。takeaways_ja: 今日のまとめ 3 項目（各 25 字以内）。

JSON だけを返してください。
"""


def check(a: dict, body: str, fig_ids: list[str]) -> list[str]:
    """根拠の実在・本文に無い数値・図の id を検査する（空なら合格）。"""
    from pfe.analyze import match_key, quote_ok, unsupported_numbers
    from pfe.overview import _with_point
    src = match_key(body)
    errs = []
    steps = a.get("steps", [])
    if not 1 <= len(steps) <= 6:
        errs.append(f"steps は 2〜5 コマ（今 {len(steps)}）")
    for i, st in enumerate(steps):
        evs = st.get("evidence") or []
        if not evs:
            errs.append(f"steps[{i}]: evidence が無い（本文から引用する）")
        for ev in evs:
            if ev.get("source") != "text":
                errs.append(f"steps[{i}]: source は \"text\" だけ")
            elif not quote_ok(ev.get("quote", ""), src):
                errs.append(f"steps[{i}]: 引用「{ev.get('quote', '')[:40]}…」が本文に見つからない（そのまま写す）")
        bad = unsupported_numbers(_with_point(st), {"text": body}, set())
        if bad:
            errs.append(f"steps[{i}]: 数値 {', '.join(bad)} が本文に無い")
        for f in st.get("figs") or []:
            if f not in fig_ids:
                errs.append(f"steps[{i}]: figs の {f} はこの論文の図・表に無い")
    return errs


def analyze_conclusion(work: Path, title: str, text: dict, fig_ids: list[str], driver: str) -> dict | None:
    """台本を作る（作成済みで材料が変わっていなければ使い回す・API の料金を二重に払わない）。作れなければ None。"""
    figdir = work / "fig"
    figdir.mkdir(exist_ok=True)
    path = figdir / "conclusion.analysis.json"
    source, where = conclusion_source(text)
    if len(source) < 200:
        print("  結論: 本文が短すぎるので作らない")
        return None
    digest = fig_digest(work, fig_ids)
    key = hashlib.sha1(json.dumps([source, digest], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    if path.exists():
        a = json.loads(path.read_text(encoding="utf-8"))
        if a.get("key") == key or driver == "manual":
            return a
    prompt = build_prompt(title, source, where, digest)
    (figdir / "conclusion.prompt.md").write_text(prompt, encoding="utf-8")
    if driver != "api":
        print("  結論: API の口でないので作らない（manual は fig/conclusion.analysis.json を書く）")
        return None
    from pfe.analyze import DriverError, call_api
    schema_path = figdir / "conclusion.schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    raw = figdir / "conclusion.analysis.raw.json"
    body = body_text(text)
    a, errs = None, []
    for attempt in range(2):                  # 検査に落ちたら、指摘を添えて 1 回だけ頼み直す
        p = prompt if not errs else prompt + "\n\n## 前回の答えの問題（直して全体を出し直す）\n" + "\n".join(f"- {e}" for e in errs)
        try:
            a = call_api(p, [], schema_path, raw, model=TEXT_MODEL)
        except DriverError as e:
            print(f"  結論: API の呼び出しに失敗: {e}")
            return None
        errs = check(a, body, fig_ids)
        print(f"  結論: 検査の指摘 {len(errs)} 件")
        if not errs:
            break
    a["key"], a["errors"], a["where"], a["source_text"] = key, errs, where, source
    path.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
    return a


def conclusion_entry(work: Path, a: dict, text: dict, where_of: dict[str, str], fig_ids: list[str],
                     no_tts: bool) -> dict:
    """bundle["conclusion"]（player が図の列の最後に「結論」として並べる。概要と同じ形）。"""
    from pfe.analyze import match_key, quote_ok, unsupported_numbers
    from pfe.overview import _with_point
    figs ={f["id"]: f for f in json.loads((work / "figures.json").read_text(encoding="utf-8"))}
    labels = {fid: figs.get(fid, {}).get("label") or fid for fid in fig_ids}
    used = [f for f in fig_ids if any(f in (st.get("figs") or []) for st in a["steps"])]
    ids = used[:MAX_MAP]
    out_png = work / "fig" / "conclusion.png"
    if ids:
        boxes = compose_map(work, ids, labels, out_png)
    else:                                         # 根拠の図が無い: 白い画面（字幕と声だけ）
        from PIL import Image
        Image.new("RGB", (1800, 1080), "white").save(out_png)
        boxes = {}
    body = body_text(text)
    nbody = match_key(body)
    # 引用がどの段落にあるか（「4 Conclusions 第2段落」のような場所の名前を出す）
    paras = [(p["id"], match_key(p.get("text") or "")) for p in text.get("paragraphs", [])]

    def where(q: str) -> str:
        for pid, t in paras:
            if q in t:
                return where_of.get(pid, "本文")
        return "本文"

    steps = []
    for st in a["steps"]:
        evs = []
        for e in st.get("evidence", []):
            q = match_key(e.get("quote", ""))
            ok = quote_ok(e.get("quote", ""), nbody)
            evs.append({"source": "text", "where": where(q) if ok else "本文", "quote": e.get("quote", ""), "verified": ok})
        bad = unsupported_numbers(_with_point(st), {"text": body}, set())
        kind = "fact" if any(e["verified"] for e in evs) and not bad else "unverified"
        audio, dur = None, None
        if not no_tts:
            try:
                from pfe.tts import synth
                p, dur = synth(st["say"], work / "audio")
                audio = f"audio/{p.name}"
            except Exception as e:        # 声が作れなくても再生ページは作る
                print(f"  結論: 音声を作れなかった: {e}")
        shapes = [{"kind": "box", "box": boxes[f], "label": ""} for f in (st.get("figs") or []) if f in boxes]
        steps.append({"say": st["say"], "caption_html": st.get("caption_html") or st["say"],
                      "point": st.get("point"), "kind": kind, "shapes": shapes, "warnings": [],
                      "evidence": evs, "audio": audio, "duration": dur, "unsupported_numbers": bad,
                      "figs": st.get("figs") or []})
    from PIL import Image
    with Image.open(out_png) as im:
        w, h = im.size
    pages = [figs[f].get("page") for f in ids if figs.get(f, {}).get("page")]
    return {"id": "conclusion", "label": "論文の結論", "caption": "", "page": max(pages) if pages else None,
            "img": "fig/conclusion.png", "width": w, "height": h, "summary_ja": a.get("summary_ja", ""),
            "takeaways_ja": a.get("takeaways_ja", []), "approx": False, "steps": steps, "refs": [],
            "calibration": {}, "map": ids, "source_text": a.get("source_text", ""),
            "source_where": a.get("where", "")}


def make_conclusion(work: Path, title: str, fig_ids: list[str], driver: str, no_tts: bool) -> dict | None:
    """run.py と scripts/add_conclusion.py の入口。text.json から結論を作って bundle["conclusion"] の形で返す。"""
    from pfe.build import para_where
    tp = work / "text.json"
    if not tp.exists() or not fig_ids:
        return None
    text = json.loads(tp.read_text(encoding="utf-8"))
    a = analyze_conclusion(work, title, text, fig_ids, driver)
    if not a or not a.get("steps"):
        return None
    return conclusion_entry(work, a, text, para_where(text), fig_ids, no_tts)
