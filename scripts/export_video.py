"""player.html を時刻ごとに撮って mp4 にする（試作の書き出し器）。

player は「時刻 t から画面が決まる」純関数（window.__player.renderAt(figIndex, t)）で描くので、
ヘッドレス Chrome で t を 1/fps ずつ進めて撮るだけでコマ落ちのない動画になる。音声は bundle の
各ステップの mp3 を、そのステップの開始時刻に置いて ffmpeg で重ねる。

使い方（playwright が入っている python 3.9 で動かす。pfe は import しない独立スクリプト）:
  python scripts/export_video.py work/<id>/player.html work/<id>/bundle.json --fig 0 --fps 15 --out work/<id>/fig1.mp4
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("player")
    ap.add_argument("bundle")
    ap.add_argument("--fig", type=int, default=0, help="何枚目の図か（0 始まり）")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-seconds", type=float, default=0, help="試し書き用に先頭だけ（0 で全部）")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    player = Path(args.player).resolve()
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    fig = bundle["figures"][args.fig]
    tmp = Path(tempfile.mkdtemp(prefix="pfe_frames_"))
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
            page.goto(player.as_uri())
            page.wait_for_function("window.__player && window.__player.figures && window.__player.figures.length > 0")
            # 書き出しの間は自動再生や音声を止め、画面の大きさを 1920x1080 に固定してもらう
            page.evaluate("window.__player.exportMode && window.__player.exportMode(true)")
            total = page.evaluate(f"window.__player.totalDuration({args.fig})")
            if args.max_seconds:
                total = min(total, args.max_seconds)
            n = int(total * args.fps) + 1
            print(f"図 {fig['label']}: {total:.1f} 秒・{n} コマ")
            # ステップの開始時刻（音声を置く位置）
            starts = page.evaluate(f"window.__player.stepStarts ? window.__player.stepStarts({args.fig}) : null")
            for i in range(n):
                t = i / args.fps
                page.evaluate(f"window.__player.renderAt({args.fig}, {t})")
                page.screenshot(path=str(tmp / f"f{i:06d}.png"))
                if i % (args.fps * 10) == 0:
                    print(f"  {t:.0f} 秒")
            b.close()

        # 映像
        silent = tmp / "silent.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(args.fps), "-i", str(tmp / "f%06d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(silent)], check=True)
        # 音声: ステップごとの mp3 を開始時刻に遅らせて重ねる
        base = Path(args.bundle).parent
        inputs, filters = [], []
        if starts:
            for k, (st, s0) in enumerate(zip(fig["steps"], starts)):
                a = st.get("audio")
                if not a or a.startswith("data:") or s0 >= total:
                    continue
                inputs += ["-i", str(base / a)]
                idx = len(inputs) // 2
                ms = int(s0 * 1000)
                filters.append(f"[{idx}:a]adelay={ms}|{ms}[a{idx}]")
        out = Path(args.out)
        if filters:
            mix = "".join(f"[a{i}]" for i in range(1, len(filters) + 1))
            fc = ";".join(filters) + f";{mix}amix=inputs={len(filters)}:normalize=0[aout]"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), *inputs, "-filter_complex", fc,
                            "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-t", f"{total:.2f}", str(out)],
                           check=True)
        else:
            shutil.copyfile(silent, out)
        print(f"できあがり: {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
