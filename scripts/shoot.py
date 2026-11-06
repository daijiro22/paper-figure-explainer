"""player.html の指定の図・指定のステップを撮って PNG にする（目視確認用）。

各ステップの「注記が描き終わったころ」（開始＋3.5 秒、ステップが短ければ末尾の手前）を撮る。
使い方（playwright が入っている python 3.9）:
  python scripts/shoot.py work/<id>/player.html --figs 2,3,6 [--steps all|3,5] --out work/<id>/shots
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("player")
    ap.add_argument("--figs", required=True, help="0 始まりの図の番号をカンマ区切り")
    ap.add_argument("--steps", default="all")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    uri = Path(args.player).resolve().as_uri()
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto(uri)
        pg.wait_for_function("window.__player && window.__player.figures && window.__player.figures.length > 0")
        pg.evaluate("window.__player.exportMode(true)")
        pg.evaluate("window.__player.preload()")
        for fi in (int(x) for x in args.figs.split(",")):
            starts = pg.evaluate(f"window.__player.stepStarts({fi})")
            total = pg.evaluate(f"window.__player.totalDuration({fi})")
            want = range(len(starts)) if args.steps == "all" else [int(x) for x in args.steps.split(",")]
            for si in want:
                end = starts[si + 1] if si + 1 < len(starts) else total
                t = min(starts[si] + 3.5, end - 0.2)
                pg.evaluate(f"window.__player.renderAt({fi}, {t})")
                pg.wait_for_timeout(150)
                path = out / f"fig{fi + 1:02d}_s{si}.png"
                pg.screenshot(path=str(path))
                print(path)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
