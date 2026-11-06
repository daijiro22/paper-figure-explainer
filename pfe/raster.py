"""画像として埋め込まれた図（ラスター）から、部品の候補（G 番号）を画素で切り出す。

線画の図は PDF の描画命令から部品を取れるが、画像の図には描画命令が無い。そのままだと LLM が
0〜1000 の座標を目測して枠がずれる（Jia 2017・Wang 2023 で枠の 7〜8 割が目測。施主 2026-10-30
「囲んでいるとこズレてるなって気になる」）。そこで画像から次の 2 種類の塊を取り、番号札を付けて選ばせる:
  - 色の塊: 彩度の高い画素（色つきの棒・凡例の色・色つきの文字や曲線）
  - 黒い塊: 暗い画素から長い直線（軸・枠・罫線）を消した残り（文字・数字・記号・小さな図形）
塊は画素を小さなマス目にまとめてから、隣り合うマスをつなげて作る（scipy・OpenCV を使わない）。
"""
from __future__ import annotations

from collections import deque

import numpy as np
from PIL import Image

RASTER_V = 2          # 切り出し方を変えたら上げる（run.py が幾何を作り直す合図）
MAX_REGIONS = 70      # 札が多すぎると画像が読めなくなる


def _remove_long_lines(mask: np.ndarray, min_len: int) -> np.ndarray:
    """横・縦に min_len 画素以上つながった線（軸・枠・罫線）を消す。"""
    out = mask.copy()
    for arr in (out, out.T):            # out.T は out の見え方なので、縦の線も同じ配列から消える
        for i in range(arr.shape[0]):
            row = arr[i]
            if row.sum() < min_len:
                continue
            # 連続する True の区間を探す
            d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
            starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
            for s, e in zip(starts, ends):
                if e - s >= min_len:
                    row[s:e] = False
    return out


def _dilate(mask: np.ndarray, k: int) -> np.ndarray:
    """縦横に k 画素ずつ膨らませる（字と字の隙間を埋めて、語・行を 1 つの塊にする）。"""
    out = mask.copy()
    for s in range(1, k + 1):
        out[:, s:] |= mask[:, :-s]
        out[:, :-s] |= mask[:, s:]
    m2 = out.copy()
    for s in range(1, k + 1):
        out[s:, :] |= m2[:-s, :]
        out[:-s, :] |= m2[s:, :]
    return out


def _components(mask: np.ndarray, cell: int, tight: np.ndarray | None = None) -> list[list[int]]:
    """mask を cell 画素のマス目にまとめ、8 近傍でつながったマスの外接矩形（画素）を返す。
    tight を渡すと、外接矩形をその画素（膨らませる前の mask など）で詰める。"""
    tight = mask if tight is None else tight
    H, W = mask.shape
    gh, gw = (H + cell - 1) // cell, (W + cell - 1) // cell
    pad = np.zeros((gh * cell, gw * cell), dtype=bool)
    pad[:H, :W] = mask
    grid = pad.reshape(gh, cell, gw, cell).any(axis=(1, 3))
    seen = np.zeros_like(grid)
    boxes = []
    for y0, x0 in zip(*np.nonzero(grid)):
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        ys, xs = [y0], [x0]
        while q:
            y, x = q.popleft()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
                        ys.append(ny)
                        xs.append(nx)
        # マス目の外接矩形を、マスの中の実際の画素で詰める
        bx0, by0 = min(xs) * cell, min(ys) * cell
        bx1, by1 = min(W, (max(xs) + 1) * cell), min(H, (max(ys) + 1) * cell)
        sub = tight[by0:by1, bx0:bx1]
        yy, xx = np.nonzero(sub)
        if len(yy):
            boxes.append([bx0 + int(xx.min()), by0 + int(yy.min()), bx0 + int(xx.max()) + 1, by0 + int(yy.max()) + 1,
                          len(ys)])
    return boxes


def _overlap(a, b) -> float:
    """小さい方の面積に対する重なりの割合。"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return ix * iy / small


def raster_boxes(png_path: str) -> list[list[int]]:
    """図の PNG から部品の候補の外接矩形（画素 [x0,y0,x1,y1]）を、上から下・左から右の順で返す。"""
    im = np.asarray(Image.open(png_path).convert("RGB")).astype(np.int16)
    H, W, _ = im.shape
    gray = im.mean(axis=2)
    sat = im.max(axis=2) - im.min(axis=2)
    cell = max(4, round(min(W, H) / 260))          # 1800px 幅で 5〜7px: 字どうしはつながり、行どうしは離れる
    colored = (sat > 60) & (gray < 245)
    dark = (gray < 150) & ~colored
    dark = _remove_long_lines(dark, min_len=int(0.25 * min(W, H)))

    cands = []
    for mask, kind in ((colored, "color"), (dark, "ink")):
        # 黒い塊（字）は字の隙間ぶん膨らませてからつなぐ。色の塊は膨らませない（隣の棒とくっつくので）
        grown = _dilate(mask, int(1.6 * cell)) if kind == "ink" else mask
        for x0, y0, x1, y1, n in _components(grown, cell, tight=mask):
            w, h = x1 - x0, y1 - y0
            if n < 3 or w * h < (2.5 * cell) ** 2:
                continue                              # 点・ノイズ
            if kind == "ink" and (w > 0.45 * W or h > 0.22 * H):
                continue                              # 黒い曲線の塊など（大きすぎて指す役に立たない）
            if w > 0.9 * W and h > 0.9 * H:
                continue                              # 図全体
            cands.append([x0, y0, x1, y1])

    # ほぼ同じ所を指す候補はまとめる（色の塊と、その上の黒い字が重なるときなど）
    cands.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    kept: list[list[int]] = []
    for b in cands:
        if any(_overlap(b, k) > 0.85 for k in kept):
            continue
        kept.append(b)
    kept = kept[:MAX_REGIONS]                         # 多すぎるときは大きい方を残す
    # 読む順（行ごとに左から）に並べる。行の高さは小さめの字 1 つ分
    row = max(8, H // 40)
    kept.sort(key=lambda b: ((b[1] + b[3]) // 2 // row, b[0]))
    return kept


def add_raster_regions(geom: dict, png_path: str) -> int:
    """geom["regions"] に画像から切り出した候補を足す（既存の G 番号はそのまま、続きの番号を振る）。足した数を返す。"""
    geom["regions"] = [g for g in geom.get("regions", []) if g.get("src") != "raster"]
    scale = geom["scale"]
    cx0, cy0 = geom["clip"][0], geom["clip"][1]
    n0 = len(geom["regions"])
    boxes = raster_boxes(png_path)
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        geom["regions"].append({"id": f"G{n0 + i + 1}", "src": "raster",
                                "bbox": [cx0 + x0 / scale, cy0 + y0 / scale, cx0 + x1 / scale, cy0 + y1 / scale]})
    geom["raster_v"] = RASTER_V
    return len(boxes)
