"""原文の画面用に、PDF のページを画像にし、図の位置をページ上の割合で返す。

ページ全体の画像は有料誌の本文そのものなので、Access の内側にだけ置く（web/app.py の /make 以下と、
研究室サイト scripts/export_site.py。後者は施主の判断 2026-11-06）。
画像は work/<pid>/pages/ に作り置きする（work/ は .gitignore 済み）。
"""
from __future__ import annotations

import json
from pathlib import Path

WIDTHS = (240, 1400)     # サイドバーの縮小と、中央の大きなページ


def page_info(d: Path) -> dict:
    """ページ数・各ページの縦横比・図の位置（ページに対する 0〜1 の割合）。"""
    import pymupdf
    doc = pymupdf.open(d / "source.pdf")
    sizes = [(p.rect.width, p.rect.height) for p in doc]
    figs = json.loads((d / "figures.json").read_text(encoding="utf-8")) if (d / "figures.json").exists() else []
    done = set()
    if (d / "bundle.json").exists():
        done = {f["id"] for f in json.loads((d / "bundle.json").read_text(encoding="utf-8")).get("figures", [])}
    out = []
    for f in figs:
        n = f.get("page")
        if not n or not f.get("clip") or not (1 <= n <= len(sizes)):
            continue
        w, h = sizes[n - 1]
        x0, y0, x1, y1 = f["clip"]
        out.append({"id": f["id"], "label": f.get("label") or f["id"], "page": n, "kind": f.get("kind", "figure"),
                    "box": [round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)],
                    "has_player": f["id"] in done})
    return {"n_pages": len(sizes), "aspect": [round(h / w, 4) for w, h in sizes], "figures": out}


def render_page(d: Path, n: int, width: int) -> Path:
    """n ページ目（1 から）を幅 width 画素の PNG にして返す（作り置きがあればそれ）。"""
    if width not in WIDTHS:
        raise ValueError(f"幅は {WIDTHS} のどれか")
    out = d / "pages" / f"p{n}_w{width}.png"
    if out.exists():
        return out
    import pymupdf
    doc = pymupdf.open(d / "source.pdf")
    if not 1 <= n <= doc.page_count:
        raise IndexError(n)
    page = doc[n - 1]
    zoom = width / page.rect.width
    out.parent.mkdir(exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(tmp))
    tmp.replace(out)
    return out
