"""作った解説の player.html を、今の player テンプレートで組み直す（解析・声は作り直さない）。

    python scripts/rebuild_players.py            # work/*/ を全部
    python scripts/rebuild_players.py hololignin2018 jia2017_acs

player/parts/ を直したとき、bundle.json に後から項目を足したときに使う。run.py を流すと
幾何の作り直しで解析がやり直しになる図（画像の図）があるので、それを避けて player だけ作る。
bundle.json に無い項目は次のように補う:
  - 図のページ番号（figures.json の page）
  - 人が直した題（meta_override.json）
  - 概要の画面に出すアブストラクトの全文（abstract.json）
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "player"))
from build_player import build  # noqa: E402
from pfe.meta import paper_meta  # noqa: E402

WORK = ROOT / "work"


def fill_bundle(d: Path) -> bool:
    """bundle.json に足りない項目を補う。書き換えたら True。"""
    bpath = d / "bundle.json"
    b = json.loads(bpath.read_text(encoding="utf-8"))
    changed = False
    pages = {f["id"]: f.get("page") for f in json.loads((d / "figures.json").read_text(encoding="utf-8"))} \
        if (d / "figures.json").exists() else {}
    for f in b.get("figures", []):
        if f.get("page") is None and pages.get(f["id"]) is not None:
            f["page"] = pages[f["id"]]
            changed = True
    ov = b.get("overview")
    if ov and not ov.get("abstract_text") and (d / "abstract.json").exists():      # 概要の画面に全文を出す（2026-11-05）
        text = json.loads((d / "abstract.json").read_text(encoding="utf-8")).get("text")
        if text:
            ov["abstract_text"] = text
            changed = True
    title = paper_meta(d).get("title")
    if title and (b.get("paper") or {}).get("title") != title:
        b["paper"] = {**(b.get("paper") or {}), "title": title}
        changed = True
    if changed:
        bpath.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    return changed


def main(names: list[str]) -> int:
    dirs = [WORK / n for n in names] if names else sorted(p.parent for p in WORK.glob("*/bundle.json"))
    n = 0
    for d in dirs:
        if not (d / "bundle.json").exists():
            print(f"bundle.json が無い: {d.name}")
            continue
        fixed = fill_bundle(d)
        build(str(d / "bundle.json"), str(d / "player.html"))
        n += 1
        print(f"  {d.name}{'（bundle を補った）' if fixed else ''}")
    print(f"{n} 本を組み直した")
    if n:
        try:
            from pfe.syncscan import request_scan
        except ImportError:     # 2 台の PC の同期のための部品。公開版には入っていない
            return 0
        request_scan()      # もう一台の PC へ届ける（検知を取りこぼすことがあるため）
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
