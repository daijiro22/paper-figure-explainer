"""bundle.json を組み立てる段: 図・解析・画素の図形・根拠の検査・引用・音声を 1 つにまとめる。

根拠の検査（捏造防止）: evidence の quote が出典の原文に実在するかを空白を正規化して照合する。
- 実在しない quote は verified=false。
- kind=fact なのに検証済みの根拠が 1 つも無いステップは kind=unverified（画面に [要確認]）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _norm(s: str) -> str:
    s = s.replace("­", "").replace("◦", "°")
    return re.sub(r"\s+", " ", s).strip().lower()


def para_where(text: dict) -> dict[str, str]:
    """段落 id → 「3.1 TG-MS analysis 第1段落」のような人向けの場所の名前。"""
    titles = {s["id"]: s["title"] for s in text.get("sections", [])}
    count: dict[str, int] = {}
    out = {}
    for p in text.get("paragraphs", []):
        sec = p.get("section")
        count[sec] = count.get(sec, 0) + 1
        out[p["id"]] = f"{titles.get(sec, '本文')} 第{count[sec]}段落"
    return out


def verify_steps(steps: list[dict], sources: dict[str, str], where: dict[str, str], tick_values: set | None = None) -> list[dict]:
    from pfe.analyze import MIN_QUOTE, unsupported_numbers
    norm_src = {k: _norm(v) for k, v in sources.items()}
    out = []
    for st in steps:
        evs = []
        for ev in st.get("evidence", []):
            src = norm_src.get(ev["source"], "")
            q = _norm(ev.get("quote") or "")
            ok = len(q) >= MIN_QUOTE and q in src
            loc = {"caption": "キャプション", "table": "表の中身"}.get(ev["source"]) or \
                where.get(ev["source"].split(":", 1)[-1], ev["source"])
            evs.append({"source": ev["source"], "where": loc, "quote": ev["quote"], "verified": ok})
        kind = st.get("kind", "fact")
        if kind == "fact" and not any(e["verified"] for e in evs):
            kind = "unverified"
        # 数値の照合: 本文・キャプション・目盛りに無い数値を含むステップは、一般知識の札でも [要確認] にする
        bad = unsupported_numbers(st, sources, tick_values or set())
        if bad:
            # 本文で確かめた引用があるステップの数値は、図の中の文字（パネル見出し・m/z の注記・凡例）から
            # 読んだものが多い（実測 Fig.2・6・9）。捏造とは区別して「図から読んだ数値」とし、数値を画面に出す。
            # 根拠の引用が無い（一般知識・未確認）ステップの数値は [要確認]
            kind = "figure_read" if kind == "fact" else "unverified"
        out.append({**st, "evidence": evs, "kind": kind, "unsupported_numbers": bad})
    return out


def figure_entry(fig: dict, img_rel: str, size: tuple[int, int], analysis: dict, resolved: dict,
                 sources: dict[str, str], where: dict[str, str], refs: list[dict], audio: list) -> dict:
    from pfe.analyze import tick_values_of
    steps = verify_steps(analysis.get("steps", []), sources, where, tick_values_of(analysis))
    rsteps = {s["index"]: s for s in resolved.get("steps", [])}
    approx = False
    out_steps = []
    for i, st in enumerate(steps):
        rs = rsteps.get(i, {"shapes": [], "warnings": []})
        approx = approx or any(sh.get("approx") for sh in rs["shapes"])
        a_path, a_dur = audio[i] if i < len(audio) else (None, None)
        out_steps.append({
            "say": st["say"], "caption_html": st.get("caption_html") or st["say"], "point": st.get("point"),
            "kind": st["kind"], "shapes": rs["shapes"], "warnings": rs["warnings"],
            "evidence": st["evidence"], "audio": a_path, "duration": a_dur,
            "unsupported_numbers": st.get("unsupported_numbers", []),
        })
    return {
        "id": fig["id"], "label": fig.get("label") or fig["id"], "caption": fig["caption"],
        "page": fig.get("page"),             # 図の載っているページ（図一覧と「原文を見る」に使う）
        "img": img_rel, "width": size[0], "height": size[1],
        "summary_ja": analysis.get("summary_ja", ""), "takeaways_ja": analysis.get("takeaways_ja", []),
        "approx": approx, "steps": out_steps, "refs": refs,
        "calibration": resolved.get("calibration", {}),
    }


def write_bundle(path: Path, paper: dict, figures: list[dict], overview: dict | None = None) -> None:
    b = {"paper": paper, "figures": figures}
    if overview:
        b["overview"] = overview          # 概要（アブストラクトの解説）。player が図の列の先頭に並べる
    path.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
