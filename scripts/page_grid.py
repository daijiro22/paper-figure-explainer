"""PDF のページに座標の目盛りを重ねた画像を作る（figures.json の clip を手で決めるため）。

  python scripts/page_grid.py <pid> 3 5 8     → work/_batch/grid/<pid>_p3.png など（ページ番号は 1 から）
  python scripts/page_grid.py <pid>           → ページ一覧（画像の数と文字量）だけ出す

赤の線が 100pt、薄い線が 50pt ごと。数字は PDF の座標で、clip にそのまま書ける（回転のないページの場合）。
"""
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = Path(__file__).resolve().parent.parent / "work"
ZOOM = 1.4


def main(pid, pages):
    doc = pymupdf.open(WORK / pid / "source.pdf")
    if not pages:
        for i, p in enumerate(doc, start=1):
            print(f"p{i}: 回転 {p.rotation} 大きさ {round(p.rect.width)}×{round(p.rect.height)} "
                  f"画像 {len(p.get_image_info())} 個 文字 {len(p.get_text())}")
        return
    out = WORK / "_batch" / "grid"
    out.mkdir(parents=True, exist_ok=True)
    for pno in pages:
        p = doc[pno - 1]
        pix = p.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        d = ImageDraw.Draw(im)
        for x in range(0, int(p.rect.width) + 1, 50):
            d.line([(x * ZOOM, 0), (x * ZOOM, pix.height)], fill=(255, 0, 0) if x % 100 == 0 else (255, 190, 190))
            if x % 100 == 0:
                d.text((x * ZOOM + 2, 2), str(x), fill="red")
        for y in range(0, int(p.rect.height) + 1, 50):
            d.line([(0, y * ZOOM), (pix.width, y * ZOOM)], fill=(255, 0, 0) if y % 100 == 0 else (255, 190, 190))
            if y % 100 == 0:
                d.text((2, y * ZOOM + 2), str(y), fill="red")
        name = out / f"{pid}_p{pno}.png"
        im.save(name)
        print(name)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], [int(a) for a in sys.argv[2:]])
