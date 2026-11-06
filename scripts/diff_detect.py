"""今の検出（pfe/figures.py）と、各論文の figures.json の差を並べる。figures.json は書き換えない。

検出を変えたら全論文で差を見る（ある型の修正が別の型を壊すことがある）。
使い方: python scripts/diff_detect.py [paper_id ...]   省略で work/ の全論文
"""
import json
import sys
from pathlib import Path

import pymupdf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe.figures import detect_figures  # noqa: E402

WORK = ROOT / "work"
TOL = 3.0      # 枠の端の差がこれ以下なら同じとみなす (pt)


def diff(pid: str) -> list[str]:
    d = WORK / pid
    old = {f["id"]: f for f in json.loads((d / "figures.json").read_text(encoding="utf-8"))}
    new = {f["id"]: f for f in detect_figures(pymupdf.open(d / "source.pdf"))}
    out = []
    for fid in sorted(set(old) | set(new)):
        o, n = old.get(fid), new.get(fid)
        if o is None:
            out.append(f"  + {fid} p{n['page']} {n['found']} {n['confidence']} clip={n['clip']}")
        elif n is None:
            out.append(f"  - {fid} p{o['page']}（新しい検出で消えた）")
        elif o.get("source") == "override":
            continue
        elif (int(o["page"]) != n["page"] or max(abs(a - b) for a, b in zip(o["clip"], n["clip"])) > TOL
              or o.get("confidence") != n["confidence"] or (o.get("rotate") or 0) != (n.get("rotate") or 0)):
            out.append(f"  ~ {fid} p{n['page']} {o.get('found')}/{o.get('confidence')} {o['clip']} → "
                       f"{n['found']}/{n['confidence']} {n['clip']}")
    return out


if __name__ == "__main__":
    pids = sys.argv[1:] or sorted(p.name for p in WORK.iterdir()
                                  if (p / "figures.json").exists() and (p / "source.pdf").exists())
    for pid in pids:
        lines = diff(pid)
        print(f"{pid}: {'差なし' if not lines else str(len(lines)) + ' 件'}")
        for ln in lines:
            print(ln)
