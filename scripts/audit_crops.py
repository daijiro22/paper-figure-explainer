"""切り出しの監査: 論文ごとに「図・表のあるページ」に切り出し枠（赤）とキャプション（青）を描いた一覧画像を作り、
本文中の図表番号と figures.json の突き合わせ・横向き（縦書きの文字）のページを表にする。figures.json は書き換えない。

使い方:
  python scripts/audit_crops.py <paper_id>... → work/_audit_crops/<pid>.png と summary.txt
"""
import json
import re
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
OUT = WORK / "_audit_crops"

# キャプションの頭（行頭の "Fig. 3" "Figure 3" "Table 2" "Scheme 1"）。本文の「Figure 2 shows ...」を数えないよう、
# 番号の直後が「.」「:」・行末・大文字で始まる語（Springer の "Fig. 1 Results of ..."）のものだけを数える
CAP_END_RE = re.compile(r"^\s*(Fig\.?|Figure|Table|Scheme)\s*(\d+|[IVX]+)(\s*(\.|:|\||$)|\s+[A-Z])")


def page_facts(page):
    """ページ内の縦書きの行数（横向きの表・図の目印）と、行頭にある図表番号を返す。"""
    vertical = 0
    heads = set()
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            dx, dy = ln["dir"]
            text = "".join(s["text"] for s in ln["spans"])
            if abs(dy) > 0.9 and len(text.strip()) > 3:
                vertical += 1
            m = CAP_END_RE.match(text)
            if m:
                kind = "table" if m.group(1).lower().startswith("tab") else (
                    "scheme" if m.group(1).lower().startswith("sch") else "fig")
                heads.add(f"{kind}{m.group(2)}")
    return vertical, heads


def audit(pid: str, lines: list[str]):
    d = WORK / pid
    figs = json.loads((d / "figures.json").read_text(encoding="utf-8"))
    doc = pymupdf.open(d / "source.pdf")
    by_page: dict[int, list[dict]] = {}
    for f in figs:
        by_page.setdefault(int(f["page"]), []).append(f)

    # PDF 側の図表番号（行頭のもの）と縦書きのページ
    heads_all, vert_pages = set(), []
    for i, page in enumerate(doc, start=1):
        v, heads = page_facts(page)
        heads_all |= heads
        if v >= 5:
            vert_pages.append((i, v))
    have = {f["id"] for f in figs}
    missing = sorted(h for h in heads_all if h not in have)
    lowconf = [f["id"] for f in figs if f.get("confidence") != "high"]
    # ページの端（左右 25pt 以内）まで伸びた枠: 余白の縦書き（RSC・ACS の透かし）を含んでいる疑い
    edge = [f["id"] for f in figs if f.get("clip") and (
        f["clip"][0] < 25 or f["clip"][2] > doc[int(f["page"]) - 1].rect.width - 25)]
    # 図と表の枠が同じページで重なっているもの
    overlap = []
    for pno, fs in by_page.items():
        for i, a in enumerate(fs):
            for b in fs[i + 1:]:
                if a.get("clip") and b.get("clip"):
                    inter = pymupdf.Rect(a["clip"]) & pymupdf.Rect(b["clip"])
                    small = min(pymupdf.Rect(a["clip"]).get_area(), pymupdf.Rect(b["clip"]).get_area())
                    if not inter.is_empty and small and inter.get_area() / small > 0.3:
                        overlap.append(f"{a['id']}×{b['id']}")
    lines.append(f"{pid}: figures.json {len(figs)} 件 / 行頭の番号 {len(heads_all)} 種 / "
                 f"figures.json に無い番号 {missing or 'なし'} / 低信頼 {lowconf or 'なし'} / "
                 f"縦書きの多いページ {vert_pages or 'なし'} / 端まで {edge or 'なし'} / 重なり {overlap or 'なし'}")

    # 図表のあるページと縦書きの多いページを、枠つきで並べる
    pages = sorted(set(by_page) | {p for p, _ in vert_pages})
    tiles = []
    for pno in pages:
        page = doc[pno - 1]
        zoom = 0.9
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        dr = ImageDraw.Draw(im)
        # get_pixmap は回転を反映した見た目で描くので、座標も回転行列で写す
        rot = page.rotation_matrix * pymupdf.Matrix(zoom, zoom)
        for f in by_page.get(pno, []):
            for key, color in (("clip", (230, 0, 0)), ("caption_bbox", (0, 90, 230))):
                if f.get(key):
                    r = (pymupdf.Rect(f[key]) * rot).normalize()   # 検出が崩れて上下が逆の枠があっても描く（Wise 1946）
                    dr.rectangle([r.x0, r.y0, r.x1, r.y1], outline=color, width=3)
            if f.get("clip"):
                r = pymupdf.Rect(f["clip"]) * rot
                dr.rectangle([r.x0, r.y0, r.x0 + 70, r.y0 + 18], fill=(230, 0, 0))
                dr.text((r.x0 + 3, r.y0 + 3), f["id"], fill=(255, 255, 255))
        dr.rectangle([0, 0, 60, 18], fill=(0, 0, 0))
        dr.text((4, 3), f"p{pno}", fill=(255, 255, 255))
        tiles.append(im)

    if not tiles:           # 図が 0 枚の論文（Nishikiori）は一覧を作らず、上の 1 行だけ残す
        return
    # 4 列で 1 枚に並べる
    cols = 4
    w = max(t.width for t in tiles)
    h = max(t.height for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), "white")
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * w, (i // cols) * h))
    OUT.mkdir(exist_ok=True)
    sheet.save(OUT / f"{pid}.png")


if __name__ == "__main__":
    lines: list[str] = []
    for pid in sys.argv[1:]:
        audit(pid, lines)
    (OUT / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
