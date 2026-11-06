"""図のベクター解析: PDF の描画命令から「プロット枠・目盛り・目盛り数字の塊・曲線・図中ラベル」を取り出す。

LLM には座標を言わせない設計（docs/design_v0.md 3.1）の土台。ここで機械的に候補を作り、
番号（T=目盛り, C=曲線, L=図中ラベル）を振って LLM に値や名前だけを答えさせる。

座標系: すべて PDF のポイント（page 座標）で持ち、画素への変換は呼び出し側が clip と scale で行う。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import pymupdf


# ---- 小道具 ---------------------------------------------------------------

def _rgb_hex(c) -> str | None:
    """PyMuPDF の (r,g,b) 0..1 を #rrggbb に。None はそのまま。"""
    if c is None:
        return None
    r, g, b = (max(0, min(255, round(v * 255))) for v in c[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _is_gray(c, tol=0.08) -> bool:
    """黒・灰・白（彩度が無い色）かどうか。曲線（色つき）と軸・文字（黒）を分ける。"""
    if c is None:
        return True
    r, g, b = c[:3]
    return max(r, g, b) - min(r, g, b) < tol


def _bezier_points(p0, p1, p2, p3, n=8):
    """3 次ベジェを n 分割した点列（始点を含まない）。"""
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0.x + 3 * mt**2 * t * p1.x + 3 * mt * t**2 * p2.x + t**3 * p3.x
        y = mt**3 * p0.y + 3 * mt**2 * t * p1.y + 3 * mt * t**2 * p2.y + t**3 * p3.y
        out.append((x, y))
    return out


def path_points(d) -> list[tuple[float, float]]:
    """描画パス 1 本を点列に展開する（線分の端点・ベジェの分割点・矩形の 4 隅）。"""
    pts: list[tuple[float, float]] = []
    for it in d["items"]:
        op = it[0]
        if op == "l":
            pts.append((it[1].x, it[1].y))
            pts.append((it[2].x, it[2].y))
        elif op == "c":
            pts.append((it[1].x, it[1].y))
            pts.extend(_bezier_points(it[1], it[2], it[3], it[4]))
        elif op == "re":
            r = it[1]
            pts.extend([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)])
        elif op == "qu":
            q = it[1]
            pts.extend([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)])
    return pts


# ---- 結果の型 -------------------------------------------------------------

@dataclass
class Tick:
    id: str
    side: str              # left | right | bottom | top
    pos: float             # 目盛りの位置（left/right なら y、bottom/top なら x）
    label_bbox: list | None = None   # 対応する数字の塊（無ければ小目盛り）


@dataclass
class Curve:
    id: str
    color: str
    bbox: list
    n_items: int
    # x を細かく区切った各区間の (x, y_top, y_bottom, y_mid)。塗りの輪郭になった曲線の中心線
    profile: list = field(default_factory=list)
    # ピーク・谷の候補 [{id, kind: peak|valley, x_pt, y_pt, prom}]
    peaks: list = field(default_factory=list)


@dataclass
class Label:
    id: str
    bbox: list
    color: str | None
    rotated: bool = False


@dataclass
class Panel:
    id: str
    frame: list            # [x0,y0,x1,y1]
    ticks: list = field(default_factory=list)
    curves: list = field(default_factory=list)
    labels: list = field(default_factory=list)


# ---- 本体 -----------------------------------------------------------------

def _find_frames(draws, clip: pymupdf.Rect) -> list[pymupdf.Rect]:
    """プロット枠の候補を探す。

    実測（Elsevier の輪郭化図）では枠は「細い黒の塗り矩形 4 本」か「白の塗り矩形＋黒の線」。
    ここでは clip 内の細長い黒の塗り（幅か高さが 1.5pt 未満で長さ 30pt 以上）を縦線・横線として集め、
    縦 2 本＋横 2 本が囲む矩形を枠とみなす。
    """
    hs, vs = [], []
    for d in draws:
        r = d["rect"]
        if not _touches(clip, r):
            continue
        col = d.get("fill") if d.get("fill") is not None else d.get("color")
        if col is None or not _is_gray(col) or sum(col[:3]) > 1.5:
            continue  # 黒っぽい線だけ
        # 1 本のパスが複数の線分を含む場合（枠全体が 1 パス）も各線分で見る
        for it in d["items"]:
            if it[0] == "re":
                rr = it[1]
            elif it[0] == "l":
                rr = pymupdf.Rect(it[1], it[2]).normalize()
            else:
                continue
            w, h = rr.width, rr.height
            if h < 1.5 and w >= 30:
                hs.append(rr)
            elif w < 1.5 and h >= 30:
                vs.append(rr)
        if d.get("type") in ("s",) and d.get("width"):
            pass
    frames = []
    # 縦線の組 × 横線の組で矩形を作る（縦の長さ≒横線間の距離）
    for i, a in enumerate(vs):
        for b in vs[i + 1:]:
            if abs(a.x0 - b.x0) < 20:
                continue
            left, right = (a, b) if a.x0 < b.x0 else (b, a)
            y0 = max(left.y0, right.y0)
            y1 = min(left.y1, right.y1)
            if y1 - y0 < 30:
                continue
            # 上下の横線がこの区間に掛かっているか
            top = [h for h in hs if abs(h.y0 - y0) < 3 and h.x0 <= left.x1 + 3 and h.x1 >= right.x0 - 3]
            bot = [h for h in hs if abs(h.y1 - y1) < 3 and h.x0 <= left.x1 + 3 and h.x1 >= right.x0 - 3]
            if bot:  # 上の線が無い（L 字の軸）も許す
                frames.append(pymupdf.Rect(left.x0, y0, right.x1, y1))
    # 右の縦軸が黒でない L 字（Huang 2024 は右の DSC・DTG 軸が緑と青）: 左の縦線の下端から右へ延びる横線で閉じる
    if not frames:
        for v in vs:
            for h in hs:
                if abs(h.y1 - v.y1) < 3 and abs(h.x0 - v.x0) < 3 and h.x1 - v.x0 >= 60 and v.height >= 60:
                    frames.append(pymupdf.Rect(v.x0, v.y0, h.x1, v.y1))
    # 重複を除く（ほぼ同じ矩形）
    uniq: list[pymupdf.Rect] = []
    for f in sorted(frames, key=lambda r: -r.get_area()):
        if any(abs(f.x0 - u.x0) < 3 and abs(f.y0 - u.y0) < 3 and abs(f.x1 - u.x1) < 3 and abs(f.y1 - u.y1) < 3 for u in uniq):
            continue
        # 他の枠の内側にすっぽり入る小さいもの（凡例の枠など）は除く
        if any(u.contains(f) and f.get_area() < 0.3 * u.get_area() for u in uniq):
            continue
        uniq.append(f)
    return uniq


def _tick_marks(draws, frame: pymupdf.Rect) -> dict[str, list[float]]:
    """枠の内側/外側に接する短い線（目盛り）の位置を辺ごとに集める。"""
    out = {"left": [], "right": [], "bottom": [], "top": []}
    for d in draws:
        col = d.get("fill") if d.get("fill") is not None else d.get("color")
        if col is None or not _is_gray(col) or sum(col[:3]) > 1.5:
            continue
        for it in d["items"]:
            if it[0] == "re":
                rr = it[1]
            elif it[0] == "l":
                rr = pymupdf.Rect(it[1], it[2]).normalize()
            else:
                continue
            w, h = rr.width, rr.height
            cx, cy = (rr.x0 + rr.x1) / 2, (rr.y0 + rr.y1) / 2
            # 横向きの短線 → 左右の辺の目盛り
            if h < 1.2 and 0.8 <= w <= 6 and frame.y0 - 1 <= cy <= frame.y1 + 1:
                if abs(rr.x0 - frame.x0) < 4 or abs(rr.x1 - frame.x0) < 4:
                    out["left"].append(cy)
                elif abs(rr.x1 - frame.x1) < 4 or abs(rr.x0 - frame.x1) < 4:
                    out["right"].append(cy)
            # 縦向きの短線 → 上下の辺の目盛り
            if w < 1.2 and 0.8 <= h <= 6 and frame.x0 - 1 <= cx <= frame.x1 + 1:
                if abs(rr.y1 - frame.y1) < 4 or abs(rr.y0 - frame.y1) < 4:
                    out["bottom"].append(cx)
                elif abs(rr.y0 - frame.y0) < 4 or abs(rr.y1 - frame.y0) < 4:
                    out["top"].append(cx)
    for k in out:
        vals = sorted(out[k])
        merged: list[float] = []
        for v in vals:
            if merged and abs(v - merged[-1]) < 1.5:  # 内向き・外向きの目盛りや隣のパネルの目盛りが 1pt 差で重なる（実測 Fig.9）
                continue
            merged.append(v)
        out[k] = merged
    return out


def _glyph_clusters(draws, texts, region: pymupdf.Rect, frame: pymupdf.Rect) -> list[dict]:
    """文字の塊（輪郭化された字の塗り、または本物の文字 span）を集めて近いもの同士をまとめる。

    曲線（大きな色つきパス）・枠線は除く。返り値は {bbox, color, n} の並び。
    """
    boxes = []
    for d in draws:
        r = d["rect"]
        if not region.intersects(r):
            continue
        if r.width > 40 and r.height > 40:
            continue  # 曲線や枠
        if len(d["items"]) > 600:
            continue  # 曲線（細い区間に入る曲線もあるので件数でも切る）
        col = d.get("fill") if d.get("fill") is not None else d.get("color")
        # 枠や目盛りの線（極端に細長い）は字ではない
        if (r.width < 1.5 and r.height > 3) or (r.height < 1.5 and r.width > 3):
            continue
        # 直線と矩形だけでできた大きめのパス（目盛りの束・枠）は字ではない。字の輪郭はベジェを含む
        if all(it[0] in ("l", "re") for it in d["items"]) and (r.width > 12 or r.height > 12):
            continue
        if r.width < 0.3 and r.height < 0.3:
            continue
        boxes.append((pymupdf.Rect(r), _rgb_hex(col)))
    for t in texts:
        r = pymupdf.Rect(t["bbox"])
        if region.intersects(r) and t["text"].strip():
            boxes.append((r, _rgb_hex(t.get("color_rgb"))))
    # 近い箱をまとめる（字間 ≒ 字の高さの 0.6 倍まで）
    clusters: list[list] = []
    for r, c in sorted(boxes, key=lambda b: (b[0].y0, b[0].x0)):
        placed = False
        for cl in clusters:
            cr = cl[0]
            gap = 0.6 * max(1.5, min(r.height, cr.height))
            grown = pymupdf.Rect(cr.x0 - gap, cr.y0 - gap * 0.5, cr.x1 + gap, cr.y1 + gap * 0.5)
            if grown.intersects(r) and (cl[1] == c or c is None or cl[1] is None):
                cl[0] = cr | r
                cl[2] += 1
                placed = True
                break
        if not placed:
            clusters.append([pymupdf.Rect(r), c, 1])
    # まとめた結果どうしがさらに重なる場合をもう 1 周まとめる
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if a[0].intersects(b[0]) and a[1] == b[1]:
                    a[0] = a[0] | b[0]
                    a[2] += b[2]
                    clusters.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [{"bbox": [cl[0].x0, cl[0].y0, cl[0].x1, cl[0].y1], "color": cl[1], "n": cl[2]} for cl in clusters]


def _curve_profile(d, frame: pymupdf.Rect, step: float) -> list:
    """塗りの輪郭になった曲線を x の区間ごとに上端・下端・中央にまとめる（中心線の近似）。"""
    # 横だけでなく縦もパネルの範囲で切る（1 本のパスが上下のパネルにまたがる subpath を持つと、
    # 両方の点が混ざって実在しない中心線ができる。Codex レビュー 2026-10-28）
    pts = [p for p in path_points(d) if frame.x0 - 2 <= p[0] <= frame.x1 + 2 and frame.y0 - 2 <= p[1] <= frame.y1 + 2]
    if not pts:
        return []
    bins: dict[int, list[float]] = {}
    for x, y in pts:
        bins.setdefault(int((x - frame.x0) / step), []).append(y)
    prof = []
    for k in sorted(bins):
        ys = bins[k]
        top, bot = min(ys), max(ys)
        prof.append([round(frame.x0 + (k + 0.5) * step, 3), round(top, 3), round(bot, 3), round((top + bot) / 2, 3)])
    return prof


def find_peak_candidates(profile: list, max_n: int = 6, min_prom: float = 0.06) -> list[dict]:
    """曲線の中心線から、ピーク（紙面で上に凸）と谷（下に凸）の候補を探す。

    紙面座標は下向きが +y なので、上に凸＝mid の極小。振れ幅（曲線全体の y の幅）に対する
    突出度（prominence）が min_prom 以上のものだけ残し、突出度の大きい順に max_n 個まで。
    LLM はこの候補に振った K 番号を選ぶだけでよい（Codex 指摘: ピークも番号で選ばせる）。
    """
    if len(profile) < 9:
        return []
    ys = [p[3] for p in profile]
    half = max(1, len(ys) // 100)
    sm = [sum(ys[max(0, i - half): i + half + 1]) / len(ys[max(0, i - half): i + half + 1]) for i in range(len(ys))]
    span = (max(sm) - min(sm)) or 1.0
    out = []
    for kind, sign in (("peak", -1), ("valley", 1)):
        # sign*sm の極大を探す（peak なら -mid の極大＝紙面で一番上）
        v = [sign * s for s in sm]
        w = max(2, len(v) // 40)
        for i in range(len(v)):
            lo, hi = max(0, i - w), min(len(v), i + w + 1)
            if v[i] < max(v[lo:hi]) or i in (0, len(v) - 1):
                continue
            # 突出度: 左右それぞれ、自分より高い点に当たるまでの最低値との差の小さいほう
            left_min = v[i]
            for j in range(i - 1, -1, -1):
                if v[j] > v[i]:
                    break
                left_min = min(left_min, v[j])
            right_min = v[i]
            for j in range(i + 1, len(v)):
                if v[j] > v[i]:
                    break
                right_min = min(right_min, v[j])
            prom = (v[i] - max(left_min, right_min)) / span
            if prom >= min_prom:
                out.append({"kind": kind, "x_pt": profile[i][0], "y_pt": sm[i], "prom": round(prom, 3)})
    # 近すぎる重複（同じ平らな頂上で複数）を除く
    out.sort(key=lambda d: -d["prom"])
    kept: list[dict] = []
    xspan = (profile[-1][0] - profile[0][0]) or 1.0
    for d in out:
        if any(k["kind"] == d["kind"] and abs(k["x_pt"] - d["x_pt"]) < 0.02 * xspan for k in kept):
            continue
        kept.append(d)
    return kept[:max_n]


def _touches(clip: pymupdf.Rect, r: pymupdf.Rect) -> bool:
    """clip と r が重なるか。幅か高さが 0 の線（1 本だけの水平・垂直線のパス）は pymupdf では「空の矩形」で
    intersects が常に False になり、Springer（Huang 2024）の軸の線が丸ごと落ちていた。0.5pt 太らせて比べる。"""
    return clip.intersects(pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5))


def analyze_figure(page: pymupdf.Page, clip: pymupdf.Rect) -> dict:
    """図 1 枚（clip の範囲）の幾何を解析して dict で返す。"""
    draws = page.get_drawings()
    draws = [d for d in draws if _touches(clip, d["rect"])]
    # 本物の文字 span（輪郭化されていない図ならここに目盛り数字が入る）
    texts = []
    for b in page.get_text("dict", clip=clip)["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                c = s.get("color", 0)
                texts.append({"bbox": s["bbox"], "text": s["text"],
                              "color_rgb": ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255),
                              "dir": l["dir"]})

    frames = _find_frames(draws, clip)
    panels = []
    used_curves: set[int] = set()
    for pi, fr in enumerate(sorted(frames, key=lambda r: (round(r.y0 / 20), r.x0))):
        pid = f"P{pi + 1}"
        marks = _tick_marks(draws, fr)
        # 文字の塊は枠の周り（外側 30pt 程度）＋内側
        around = pymupdf.Rect(fr.x0 - 35, fr.y0 - 15, fr.x1 + 35, fr.y1 + 30) & clip
        glyphs = _glyph_clusters(draws, texts, around, fr)

        ticks: list[Tick] = []
        n = 0
        for side, positions in marks.items():
            for pos in positions:
                n += 1
                # この目盛りに対応する数字の塊（辺の外側にあり、目盛り位置に中心が近いもの）
                best, best_d = None, 1e9
                for g in glyphs:
                    x0, y0, x1, y1 = g["bbox"]
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    if side == "left" and x1 <= fr.x0 + 1 and fr.x0 - x1 < 12:
                        dist = abs(cy - pos)
                    elif side == "right" and x0 >= fr.x1 - 1 and x0 - fr.x1 < 12:
                        dist = abs(cy - pos)
                    elif side == "bottom" and y0 >= fr.y1 - 1 and y0 - fr.y1 < 12:
                        dist = abs(cx - pos)
                    elif side == "top" and y1 <= fr.y0 + 1 and fr.y0 - y1 < 12:
                        dist = abs(cx - pos)
                    else:
                        continue
                    tol = max(2.5, 0.6 * ((y1 - y0) if side in ("left", "right") else (x1 - x0)))
                    if dist < tol and dist < best_d:
                        best, best_d = g, dist
                ticks.append(Tick(id="", side=side, pos=round(pos, 3), label_bbox=best["bbox"] if best else None))

        # 1 つの数字の塊は、最も近い目盛り 1 本にだけ付ける（近い目盛りが 2 本あると同じ数字が両方に付き、
        # LLM が隣の目盛りに値を答えて較正が崩れる。実測 Fig.9 の 2x4 パネル）
        best_for: dict = {}
        for t in ticks:
            if t.label_bbox:
                b = t.label_bbox
                c = (b[0] + b[2]) / 2 if t.side in ("bottom", "top") else (b[1] + b[3]) / 2
                d = abs(c - t.pos)
                k = tuple(b)
                if k not in best_for or d < best_for[k][0]:
                    best_for[k] = (d, id(t))
        for t in ticks:
            if t.label_bbox and best_for[tuple(t.label_bbox)][1] != id(t):
                t.label_bbox = None

        # 目盛りの線が無い軸（数字だけが並ぶ）: 枠の外側すぐに、数字の大きさの塊が 3 個以上一列に並んでいれば、
        # その塊の中心を目盛りの位置とみなす（実測: Taira 2026 Fig.2 の m/z 軸は目盛りの線が無かった）
        used = {tuple(t.label_bbox) for t in ticks if t.label_bbox}
        for side in ("bottom", "left"):
            if sum(1 for t in ticks if t.side == side and t.label_bbox) >= 3:
                continue  # 目盛りの線と数字が十分に結び付いている軸はそのまま
            cand = []
            for g in glyphs:
                x0, y0, x1, y1 = g["bbox"]
                if tuple(g["bbox"]) in used or (y1 - y0) > 12 or (x1 - x0) > 30:
                    continue
                if side == "bottom" and 0 <= y0 - fr.y1 < 12 and fr.x0 - 8 <= (x0 + x1) / 2 <= fr.x1 + 8:
                    cand.append(g)
                if side == "left" and 0 <= fr.x0 - x1 < 12 and fr.y0 - 8 <= (y0 + y1) / 2 <= fr.y1 + 8:
                    cand.append(g)
            if len(cand) < 3:
                continue
            # 一列に並んでいるか（下なら y の中心、左なら右端がそろう）
            key = (lambda b: (b[1] + b[3]) / 2) if side == "bottom" else (lambda b: b[2])
            ref = sorted(key(g["bbox"]) for g in cand)[len(cand) // 2]
            row = [g for g in cand if abs(key(g["bbox"]) - ref) < 1.5]
            if len(row) < 3:
                continue
            for g in row:
                b = g["bbox"]
                pos = (b[0] + b[2]) / 2 if side == "bottom" else (b[1] + b[3]) / 2
                ticks.append(Tick(id="", side=side, pos=round(pos, 3), label_bbox=b))

        # 曲線: 枠の中にある、彩度のある大きな塗り/線（または点数の多い黒い線）
        curves: list[Curve] = []
        for di, d in enumerate(draws):
            if di in used_curves:
                continue
            r = d["rect"]
            inner = r & fr
            if inner.is_empty or inner.get_area() < 0.5 * max(1e-6, r.get_area()) * 0.5:
                continue
            col = d.get("fill") if d.get("fill") is not None else d.get("color")
            if len(d["items"]) < 40:
                continue
            if r.width < fr.width * 0.3:
                continue  # 字や小さな記号（色つきの凡例の字もここで落ちる）
            if _is_gray(col) and len(d["items"]) < 300:
                continue
            used_curves.add(di)
            curves.append(Curve(id="", color=_rgb_hex(col) or "#000000",
                                bbox=[r.x0, r.y0, r.x1, r.y1], n_items=len(d["items"]),
                                profile=_curve_profile(d, fr, step=max(0.25, fr.width / 400))))

        # 図中ラベル（数字の塊として目盛りに使われなかった塊）
        tick_boxes = [tuple(t.label_bbox) for t in ticks if t.label_bbox]
        labels = []
        for g in glyphs:
            if tuple(g["bbox"]) in tick_boxes:
                continue
            x0, y0, x1, y1 = g["bbox"]
            labels.append(Label(id="", bbox=g["bbox"], color=g["color"], rotated=(y1 - y0) > 2.5 * (x1 - x0)))

        panels.append(Panel(id=pid, frame=[fr.x0, fr.y0, fr.x1, fr.y1],
                            ticks=ticks, curves=curves, labels=labels))

    # 図全体で通し番号を振る（LLM が参照しやすいよう T1.., C1.., L1..）
    t_no = c_no = l_no = k_no = 0
    for p in panels:
        for t in p.ticks:
            if t.label_bbox:
                t_no += 1
                t.id = f"T{t_no}"
        for c in p.curves:
            c_no += 1
            c.id = f"C{c_no}"
            # ピーク・谷の候補（K 番号）。曲線ごとに持たせる
            c.peaks = find_peak_candidates(c.profile)
            for k in c.peaks:
                k_no += 1
                k["id"] = f"K{k_no}"
        for l in p.labels:
            l_no += 1
            l.id = f"L{l_no}"

    kind = "vector" if panels and any(p.curves or p.ticks for p in panels) else (
        "raster" if any(clip.intersects(pymupdf.Rect(im["bbox"])) for im in page.get_image_info()) else "vector_noaxes")
    # 軸の無い図（機構図・構造式・写真）: 描画のまとまり（部品）に G 番号を振って、
    # LLM が「G3 を囲む」「G2 から G5 へ矢印」と選べるようにする
    regions = []
    if not panels:
        for i, bb in enumerate(_object_groups(draws, page, clip)):
            regions.append({"id": f"G{i + 1}", "bbox": bb})
    return {"clip": [clip.x0, clip.y0, clip.x1, clip.y1], "kind": kind,
            "panels": [asdict(p) for p in panels], "regions": regions}


def _object_groups(draws, page, clip: pymupdf.Rect, gap: float = 4.0, max_n: int = 40) -> list[list[float]]:
    """描画（と埋め込み画像）を、隙間 gap pt 以内で触れ合うもの同士まとめた外接矩形の並び。

    機構図なら構造式 1 つ・矢印 1 本・化合物番号などがそれぞれ 1 つのまとまりになりやすい。
    小さすぎるもの（点・字 1 文字）は近くのまとまりに吸収されなければ捨てる。
    """
    # 背景の塗り（図の 8% 以上を覆う、直線・矩形だけの面）は部品ではないので外す。
    # 入れると全部が 1 つにまとまる（実測: Taira 2026 Fig.4 の薄青の背景 2 枚）
    total_area = clip.get_area()
    def _is_background(d):
        r = pymupdf.Rect(d["rect"]) & clip
        return (r.get_area() > 0.08 * total_area and d.get("fill") is not None
                and all(it[0] in ("re", "l", "qu") for it in d["items"]) and len(d["items"]) <= 8)
    # 矢印・括弧のような「長い直線だけの経路」は部品どうしをつないで全体を 1 つにしてしまうので、
    # 合併には使わず、それ自体を 1 つの部品として残す（実測: Fig.4 の反応の矢印と括弧線）。
    # 構造式の結合も直線だが短い（数 pt）ので、図の短辺の 10% より長いものだけを対象にする
    long_len = 0.10 * min(clip.width, clip.height)
    def _is_connector(d):
        r = pymupdf.Rect(d["rect"])
        diag = math.hypot(r.width, r.height)
        if len(d["items"]) > 12 or diag <= 0.8 * long_len:
            return False
        if all(it[0] in ("l", "re") for it in d["items"]):
            return True  # 直線の矢印（斜めも含む。斜めは外接矩形の対角で長さを見る）
        # 括弧（波括弧・角括弧）: ベジェを含むが、とても細長い
        return min(r.width, r.height) < 3.0
    connectors = [pymupdf.Rect(d["rect"]) & clip for d in draws if not _is_background(d) and _is_connector(d)]
    rects = [pymupdf.Rect(d["rect"]) & clip for d in draws if not _is_background(d) and not _is_connector(d)]
    rects += [pymupdf.Rect(im["bbox"]) & clip for im in page.get_image_info()]
    for b in page.get_text("dict", clip=clip)["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip():
                    rects.append(pymupdf.Rect(s["bbox"]))
    rects = [r for r in rects if not r.is_empty and r.get_area() > 0.2]
    # 素朴な合併（図 1 枚なら数千個以内なので O(n^2) で足りる）
    groups: list[pymupdf.Rect] = []
    for r in sorted(rects, key=lambda r: (r.y0, r.x0)):
        g = pymupdf.Rect(r.x0 - gap, r.y0 - gap, r.x1 + gap, r.y1 + gap)
        hit = [i for i, q in enumerate(groups) if q.intersects(g)]
        if not hit:
            groups.append(pymupdf.Rect(r))
            continue
        merged = pymupdf.Rect(r)
        for i in sorted(hit, reverse=True):
            merged |= groups.pop(i)
        groups.append(merged)
    # 合併で大きくなったもの同士がさらに触れる場合をまとめ直す
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if pymupdf.Rect(a.x0 - gap, a.y0 - gap, a.x1 + gap, a.y1 + gap).intersects(b):
                    groups[i] = a | b
                    groups.pop(j)
                    changed = True
                    break
            if changed:
                break
    total = clip.get_area()
    # 矢印の頭（小さな三角の塗り）が残っていれば、それに触れる connector に吸収させる
    for c in connectors:
        cg = pymupdf.Rect(c.x0 - 1.5, c.y0 - 1.5, c.x1 + 1.5, c.y1 + 1.5)
        heads = [i for i, g in enumerate(groups) if g.get_area() < total * 0.002 and g.intersects(cg)]
        for i in sorted(heads, reverse=True):
            c |= groups.pop(i)
        groups.append(c)
    # 化合物番号（太字の 2 桁）のような小さな塊も意味があるので、下限は小さめ（図の 0.05%）
    keep = [g for g in groups if g.get_area() > total * 0.0002 and g.get_area() < total * 0.9]
    keep.sort(key=lambda r: (round(r.y0 / 15), r.x0))
    return [[g.x0, g.y0, g.x1, g.y1] for g in keep[:max_n]]
