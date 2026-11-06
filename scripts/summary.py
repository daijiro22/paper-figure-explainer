"""作業フォルダ（work/<id>）ごとに、通し実行の結果を 1 行の表にまとめる（別の型の論文で動くかの点検用）。

使い方:
  py -3.13 scripts/summary.py work/wang2023_rsc work/jia2017_acs ...   （Markdown の表を標準出力へ）

数えるもの:
  - 図: 検出した図の数（fig*）・本文で言及された図番号のうち検出できなかったもの・low の数
  - 解析: 解析済みの図（analysis.json）・bundle に入った図
  - resolve の警告の数（*.resolved.json の steps[].warnings）
  - ステップの種類: [要確認]（unverified）・図から読んだ数値（figure_read）・引用が原文に無い（evidence.verified=False）
  - 図形: 赤丸の数・近似（approx）の図形の数・枠（panel）の有無
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import re
from pathlib import Path


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def summarize(work: Path) -> dict:
    figs = _load(work / "figures.json") or []
    text = _load(work / "text.json") or {}
    bundle = _load(work / "bundle.json") or {"figures": []}
    det = {f["id"] for f in figs if re.fullmatch(r"fig\d+", f["id"])}
    mentioned = {k for k in text.get("fig_mentions", {}) if re.fullmatch(r"fig\d+", k)}
    row = {
        "id": work.name,
        "detected": len(det),
        "missed": sorted(mentioned - det, key=lambda s: int(s[3:])),
        "low": sum(1 for f in figs if f["id"] in det and f.get("confidence") == "low"),
        "analyzed": 0, "in_bundle": len(bundle["figures"]), "warnings": 0,
        "steps": 0, "unverified": 0, "figure_read": 0, "bad_quote": 0,
        "circles": 0, "approx": 0, "with_panel": 0, "per_fig": [],
    }
    for f in bundle["figures"]:
        fid = f["id"]
        geom = _load(work / "fig" / f"{fid}.geom.json") or {}
        res = _load(work / "fig" / f"{fid}.resolved.json") or {"steps": []}
        if (work / "fig" / f"{fid}.analysis.json").exists():
            row["analyzed"] += 1
        nw = sum(len(s.get("warnings", [])) for s in res["steps"])
        kinds = [s.get("kind") for s in f["steps"]]
        shapes = [sh for s in f["steps"] for sh in s.get("shapes", [])]
        pf = {
            "fig": fid, "kind": geom.get("kind", "?"), "panels": len(geom.get("panels", [])),
            "steps": len(kinds), "warn": nw,
            "unverified": kinds.count("unverified"), "figure_read": kinds.count("figure_read"),
            "bad_quote": sum(1 for s in f["steps"] for e in s.get("evidence", []) if e.get("verified") is False),
            "circles": sum(1 for sh in shapes if sh["kind"] == "circle"),
            "approx": sum(1 for sh in shapes if sh.get("approx")),
            "shape_kinds": sorted({sh["kind"] for sh in shapes}),
        }
        row["per_fig"].append(pf)
        for k in ("steps", "unverified", "figure_read", "bad_quote", "circles", "approx"):
            row[k] += pf[k]
        row["warnings"] += nw
        row["with_panel"] += pf["panels"] > 0
    return row


def main() -> int:
    rows = [summarize(Path(p)) for p in sys.argv[1:]]
    print("| 論文 | 図の検出 | 取りこぼし | low | 通した図 | 枠あり | resolve 警告 | ステップ | [要確認] | 図から読んだ数値 | 引用不一致 | 赤丸 | 近似 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['id']} | {r['detected']} | {','.join(r['missed']) or 'なし'} | {r['low']} | {r['in_bundle']} | "
              f"{r['with_panel']} | {r['warnings']} | {r['steps']} | {r['unverified']} | {r['figure_read']} | "
              f"{r['bad_quote']} | {r['circles']} | {r['approx']} |")
    print()
    for r in rows:
        for pf in r["per_fig"]:
            print(f"- {r['id']} {pf['fig']}: 種類={pf['kind']} 枠={pf['panels']} ステップ={pf['steps']} 警告={pf['warn']} "
                  f"要確認={pf['unverified']} 図読み={pf['figure_read']} 赤丸={pf['circles']} 近似={pf['approx']} 図形={pf['shape_kinds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
