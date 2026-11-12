"""bundle.json を組み立てる段: 図・解析・画素の図形・根拠の検査・引用・音声を 1 つにまとめる。

根拠の検査（捏造防止）: evidence の quote が出典の原文に実在するかを、全角・半角と空白の違いを無視して照合する。
- 実在しない quote は verified=false。
- kind=fact なのに検証済みの根拠が 1 つも無いステップは kind=unverified（画面に [要確認]）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _kind(fid: str) -> str:
    """id の種類（fig・table・scheme・補足の tableS は別の列）。"""
    m = re.match(r"[a-zA-Z]+", fid)
    return m.group(0) if m else fid


def _pos(f: dict) -> tuple:
    """ページの中で読む順の位置（2 段組みは左の段 → 右の段。下半分に載る全幅の図は両方の段のあと）。"""
    y = (f.get("clip") or [0, 0, 0, 0])[1]
    layout = f.get("layout")
    col = 1 if layout == "right" else (2 if layout == "full" and y > 400 else 0)   # 400 pt ≒ ページの半分
    page = f.get("page")
    return (page if page is not None else 10 ** 6, col, y)


def reading_order(figs: list[dict]) -> list[dict]:
    """図・表・Scheme を、ページの若い順に並べる（施主 2026-11-10「図のあとに Table を出されても前後して分かりづらい」）。
    種類ごとの順（図 1→2→3、表 1→表 1 の続き→表 2）は figures.json のまま崩さず、種類どうしだけを
    載っている位置で混ぜる（続きの表が座標では親より上に来る論文がある・smith2016）。"""
    queues: dict[str, list[dict]] = {}
    for f in figs:
        queues.setdefault(_kind(f["id"]), []).append(f)
    out = []
    while any(queues.values()):
        k = min((k for k, q in queues.items() if q), key=lambda k: _pos(queues[k][0]))
        out.append(queues[k].pop(0))
    return out


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
    from pfe.analyze import match_key, quote_ok, unsupported_numbers
    norm_src = {k: match_key(v) for k, v in sources.items()}
    out = []
    for st in steps:
        evs = []
        for ev in st.get("evidence", []):
            src = norm_src.get(ev["source"], "")
            ok = quote_ok(ev.get("quote") or "", src)
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


def write_bundle(path: Path, paper: dict, figures: list[dict], overview: dict | None = None,
                 conclusion: dict | None = None) -> None:
    b = {"paper": paper, "figures": figures}
    if overview:
        b["overview"] = overview          # 概要（アブストラクトの解説）。player が図の列の先頭に並べる
    if conclusion:
        b["conclusion"] = conclusion      # 結論（論文全体のまとめ）。player が図の列の最後に並べる
    path.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
