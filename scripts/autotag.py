"""論文にタグを自動で付ける（pfe/autotag.py をまとめて回す）。

    py -3.13 scripts/autotag.py                  # タグが 1 つも付いていない本だけ（まだ自動で付けていないもの）
    py -3.13 scripts/autotag.py <pid>...         # 指定した本（もう自動で付けた本は --force で付け直し・足すだけ）
    py -3.13 scripts/autotag.py --dry-run        # 付けずに、付けるタグだけ見る

Web 版は論文ができたときに自動で付ける（web/jobs.py）。これは前からある本・付け直し用。
鍵は ANTHROPIC_API_KEY か ~/.config/pfe/env。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import tags as tagstore  # noqa: E402
from pfe.autotag import auto_tag  # noqa: E402

WORK = ROOT / "work"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pids", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="もう自動で付けた本も付け直す")
    a = ap.parse_args()

    live = {t["id"] for t in tagstore.active(WORK)}
    if a.pids:
        dirs = [WORK / p for p in a.pids]
    else:
        # 一覧に出る本（player.html がある）のうち、見えるタグが 1 つも無いもの
        dirs = [d for d in sorted(WORK.iterdir())
                if d.is_dir() and not d.name.startswith("_") and (d / "player.html").exists()
                and not any(t in live for t in tagstore.paper_tags(d))]
    print(f"{len(dirs)} 本")
    n = 0
    for d in dirs:
        if not d.is_dir():
            print(f"  {d.name}: フォルダが無い")
            continue
        try:
            r = auto_tag(d, WORK, force=a.force, dry_run=a.dry_run)
        except Exception as e:
            print(f"  {d.name}: 失敗 {e!r}")
            continue
        if r is None:
            print(f"  {d.name}: 飛ばした（もう自動で付けた・題も要旨も無い）")
            continue
        n += 1
        new = f"  新: {', '.join(r['new'])}" if r["new"] else ""
        print(f"  {d.name}: {', '.join(r['existing']) or '-'}{new}")
    print(f"{'付ける案' if a.dry_run else '付けた'}: {n} 本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
