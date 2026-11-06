"""表の「幾何」: 表の中の語を、セル（語のまとまり）と行にまとめて G 番号の候補にし、表の文字を根拠の原文にする。

表は軸つきのグラフではないので、geometry.analyze_figure には通さない（罫線を軸の枠と取り違える）。
文字が PDF に入っている表は語の位置が正確に取れるので、LLM は「G12 を囲む」と番号を選ぶだけでよい。
文字が無い（画像の）表は None を返し、run.py が画像の図と同じ切り出し（pfe/raster.py）に回す。

table_text の形（依頼文にもこのまま書く）: 1 行 = 表の 1 行。セルとセルの間は空白 1 つ。上から下の順。
ナレーションの数値の照合（analyze.unsupported_numbers）と、根拠の引用（source "table"）に使う。
"""
from __future__ import annotations

MAX_REGIONS = 90      # セルが多すぎる表は、行を候補にする


def _rows(words: list) -> list[list]:
    """語を見た目の行にまとめる（縦の中心が字の高さの半分以内なら同じ行）。上から順。"""
    rows: list[list] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc, h = (w[1] + w[3]) / 2, w[3] - w[1]
        if rows:
            r = rows[-1]
            ryc = sum((x[1] + x[3]) / 2 for x in r) / len(r)
            if abs(yc - ryc) < 0.5 * max(h, 1):
                r.append(w)
                continue
        rows.append([w])
    return [sorted(r, key=lambda w: w[0]) for r in rows]


def _cells(row: list) -> list[list]:
    """1 行の語を、間が字の高さの 0.6 倍より広いところで切ってセルに分ける。"""
    cells: list[list] = []
    for w in row:
        h = w[3] - w[1]
        if cells and w[0] - cells[-1][-1][2] <= max(3.0, 0.6 * h):
            cells[-1].append(w)
        else:
            cells.append([w])
    return cells


def _box(ws: list) -> list[float]:
    return [round(min(w[0] for w in ws), 2), round(min(w[1] for w in ws), 2),
            round(max(w[2] for w in ws), 2), round(max(w[3] for w in ws), 2)]


def table_geometry(page, clip, min_words: int = 6) -> dict | None:
    """clip（PDF 座標）の中の表の幾何。文字がほとんど無ければ None（画像の表）。"""
    import pymupdf
    words = [w for w in page.get_text("words", clip=pymupdf.Rect(clip)) if w[4].strip()]
    if len(words) < min_words:
        return None
    rows = _rows(words)
    cells = [_cells(r) for r in rows]
    text = "\n".join(" ".join(" ".join(w[4] for w in c) for c in cs) for cs in cells)
    n_cells = sum(len(cs) for cs in cells)
    if n_cells <= MAX_REGIONS:
        boxes = [_box(c) for cs in cells for c in cs]
        unit = "セル"
    else:
        boxes = [_box(r) for r in rows]
        unit = "行"
    regions = [{"id": f"G{i + 1}", "bbox": b, "src": "table"} for i, b in enumerate(boxes)]
    return {"clip": list(clip), "kind": "table", "panels": [], "regions": regions,
            "table_unit": unit, "table_text": text}
