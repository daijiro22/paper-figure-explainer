"""作成済みの論文の figures.json に、表の行だけを足す・取り直す（図・スキームの行には触らない）。

    python scripts/add_tables.py            # work/*/ を全部（書き換える前に差分を出す）
    python scripts/add_tables.py --dry-run  # 差分を出すだけ

figures.json は最初の取り込みで作り置きされるので、表の検出を直しても（2026-10-30・ACS の本文と同じ大きさの表題、
表の外枠を find_tables で取る）作成済みの論文には表が増えない。run.py --force は図の画像と幾何まで作り直して
解析がやり直しになるので使わず、ここで表の行だけを入れ替える。
まだ解説を作っていない表（fig/<id>.png が無い表）だけを取り直す。作った表の切り出しは変えない。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe.figures import detect_figures  # noqa: E402

WORK = ROOT / "work"


def merge(d: Path, dry: bool) -> list[str]:
    path = d / "figures.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    new_tables = [f for f in detect_figures(pymupdf.open(d / "source.pdf")) if f["kind"] == "table"]
    done = {f["id"] for f in old if (d / "fig" / f"{f['id']}.png").exists()}
    keep = [f for f in old if f.get("kind") != "table" or f["id"] in done]    # 図と、解説を作った表はそのまま
    keep_ids = {f["id"] for f in keep}
    added = [f for f in new_tables if f["id"] not in keep_ids]
    old_t = {f["id"]: f for f in old if f.get("kind") == "table"}
    notes = []
    for f in added:
        o = old_t.get(f["id"])
        notes.append(f"{f['id']}（p{f['page']}）" + ("を取り直す" if o and o["clip"] != f["clip"] else
                                                  "は同じ" if o else "を足す"))
    out = keep + added
    assert [f["id"] for f in out if f.get("kind") != "table"] == [f["id"] for f in old if f.get("kind") != "table"]
    assert all(a == b for a, b in zip([f for f in out if f.get("kind") != "table"],
                                      [f for f in old if f.get("kind") != "table"])), "図の行が変わった"
    if not dry and out != old:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return notes


def main() -> int:
    dry = "--dry-run" in sys.argv
    for d in sorted(p.parent for p in WORK.glob("*/figures.json")):
        if not (d / "source.pdf").exists():
            continue
        notes = merge(d, dry)
        print(f"{d.name}: {'、'.join(notes) if notes else '表なし'}")
    print("（差分だけ。書き換えていない）" if dry else "figures.json を更新した")
    return 0


if __name__ == "__main__":
    sys.exit(main())
