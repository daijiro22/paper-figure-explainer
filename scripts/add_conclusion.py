"""作り終えた解説に「結論」（全部の図・表のあとに流す論文全体のまとめ）を足す（図の解析・声は作り直さない）。

    py -3.13 scripts/add_conclusion.py <pid>...       # 指定した本
    py -3.13 scripts/add_conclusion.py --all          # 結論がまだ無い本を全部
    py -3.13 scripts/add_conclusion.py <pid> --dry-run   # API を呼ばずに、材料（結論の節か本文の終わりか）だけ見る

新しい本は run.py が図・表がそろったときに作る。これは前からある本用。Claude API を 1 本 1 回呼ぶ
（鍵は ANTHROPIC_API_KEY か ~/.config/pfe/env）。作った台本は work/<pid>/fig/conclusion.analysis.json に残り、
材料が変わらなければ 2 回目は API を呼ばない。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "player"))
from build_player import build  # noqa: E402
from pfe.conclusion import conclusion_source, make_conclusion  # noqa: E402
from pfe.meta import paper_meta  # noqa: E402

WORK = ROOT / "work"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pids", nargs="*")
    ap.add_argument("--all", action="store_true", help="結論がまだ無い本を全部")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-tts", action="store_true")
    a = ap.parse_args()
    if a.pids:
        dirs = [WORK / p for p in a.pids]
    elif a.all:
        dirs = [p.parent for p in sorted(WORK.glob("*/bundle.json"))
                if not json.loads(p.read_text(encoding="utf-8")).get("conclusion")]
    else:
        ap.error("pid か --all を指定する")
    print(f"{len(dirs)} 本")
    n = 0
    for d in dirs:
        bpath = d / "bundle.json"
        if not bpath.exists() or not (d / "text.json").exists():
            print(f"  {d.name}: bundle.json か text.json が無い")
            continue
        b = json.loads(bpath.read_text(encoding="utf-8"))
        ids = [f["id"] for f in b.get("figures", [])]
        if a.dry_run:
            src, where = conclusion_source(json.loads((d / "text.json").read_text(encoding="utf-8")))
            print(f"  {d.name}: {where}・{len(src)} 字・図表 {len(ids)}")
            continue
        entry = make_conclusion(d, paper_meta(d).get("title") or d.name, ids, "api", a.no_tts)
        if not entry:
            print(f"  {d.name}: 作れなかった")
            continue
        b["conclusion"] = entry
        bpath.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
        build(str(bpath), str(d / "player.html"))
        unver = sum(1 for s in entry["steps"] if s["kind"] != "fact")
        print(f"  {d.name}: {len(entry['steps'])} コマ（[要確認] {unver}）・図 {', '.join(entry['map']) or 'なし'}")
        n += 1
    print(f"足した: {n} 本")
    if n:
        try:
            from pfe.syncscan import request_scan
        except ImportError:     # 2 台の PC の同期のための部品。公開版には入っていない
            return 0
        request_scan()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
