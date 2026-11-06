"""LLM の答え（番号と値・データ座標の指示）を、図 PNG 上の画素の描画指示に直す。

- 軸の較正: 目盛り番号 → 値 の対応と、幾何解析で得た目盛りの位置から 1 次式（対数軸なら log10）を当てる。
  残差が大きければ較正を捨てる（その軸を使う指示は「近似」扱いにして LLM の bbox 指定へ退避）。
- ピークの吸着: 曲線の中心線（x 区間ごとの中央）をデータ座標に直し、指定区間の極大/極小を探す。
- 出力は player が描くだけの単純な図形（band / circle / trace / box / arrow / zoom / axis）。
"""
from __future__ import annotations

import math
import re


class CalibrationError(Exception):
    pass


def fit_axis(pairs: list[tuple[float, float]]) -> dict:
    """(位置pt, 値) の組から軸の変換を当てる。返り値 {a, b, log, resid}（value = a*pos + b、log なら 10**(…)）。"""
    pairs = [(p, v) for p, v in pairs if v is not None]
    if len(pairs) < 2:
        raise CalibrationError("目盛りが 2 個未満")

    def lin(ps):
        n = len(ps)
        mx = sum(p for p, _ in ps) / n
        my = sum(v for _, v in ps) / n
        sxx = sum((p - mx) ** 2 for p, _ in ps)
        if sxx == 0:
            raise CalibrationError("目盛りの位置が全部同じ")
        a = sum((p - mx) * (v - my) for p, v in ps) / sxx
        b = my - a * mx
        span = max(v for _, v in ps) - min(v for _, v in ps) or 1.0
        resid = max(abs(a * p + b - v) for p, v in ps) / span
        return a, b, resid

    a, b, r = lin(pairs)
    best = {"a": a, "b": b, "log": False, "resid": r}
    if all(v > 0 for _, v in pairs) and len(pairs) >= 3:
        la, lb, lr = lin([(p, math.log10(v)) for p, v in pairs])
        # 対数のほうが明らかに当てはまるときだけ対数軸とみなす
        if lr < r * 0.3:
            best = {"a": la, "b": lb, "log": True, "resid": lr}
    best["n"] = len(pairs)
    # 1% を超える残差は読み違いとみなす（2% だと 200→210 の誤読が通った。Codex レビュー 2026-10-28）
    if best["resid"] > 0.01:
        raise CalibrationError(f"目盛りの値と位置が直線に乗らない（残差 {best['resid']:.3f}）")
    return best


def to_value(cal: dict, pos: float) -> float:
    v = cal["a"] * pos + cal["b"]
    return 10 ** v if cal["log"] else v


def to_pos(cal: dict, value: float) -> float:
    v = math.log10(value) if cal["log"] else value
    return (v - cal["b"]) / cal["a"]


def calibrate_panel(geom_panel: dict, ans_panel: dict) -> dict:
    """パネル 1 枚の各軸を較正する。返り値 {"x": cal, "y_left": cal, "y_right": cal, "errors": {...}}。"""
    tick_by_id = {t["id"]: t for t in geom_panel["ticks"] if t["id"]}
    side_of_axis = {"x": ("bottom", "top"), "x_top": ("top",), "y_left": ("left",), "y_right": ("right",)}
    out: dict = {"errors": {}}
    for axis, spec in (ans_panel.get("axes") or {}).items():
        if axis not in side_of_axis or not spec:
            continue
        pairs = []
        label_by_id = {l["id"]: l for l in geom_panel["labels"]}
        for tid, val in (spec.get("ticks") or {}).items():
            t = tick_by_id.get(tid)
            if t is not None and t["side"] in side_of_axis[axis]:
                pos = t["pos"]
            elif tid in label_by_id:
                # 目盛りの線と結び付かなかった数字の塊（L 番号）を LLM が目盛りとして読んだ場合:
                # 塊の中心を目盛りの位置とみなす（横軸なら x、縦軸なら y）。誤りは fit_axis の残差検査で弾く
                b = label_by_id[tid]["bbox"]
                pos = (b[0] + b[2]) / 2 if axis in ("x", "x_top") else (b[1] + b[3]) / 2
            else:
                continue
            try:
                pairs.append((pos, float(val)))
            except (TypeError, ValueError):
                continue
        try:
            out[axis] = fit_axis(pairs)
        except CalibrationError as e:
            out["errors"][axis] = str(e)
    return out


def curve_data(curve: dict, calx: dict, caly: dict) -> list[tuple[float, float, float, float]]:
    """曲線の中心線を (x値, y値, x_pt, y_pt) の並びにする。"""
    pts = []
    for x, top, bot, mid in curve["profile"]:
        pts.append((to_value(calx, x), to_value(caly, mid), x, mid))
    return pts


def find_extremum(curve: dict, calx: dict, caly: dict, x_range, kind: str = "max") -> dict:
    """x_range（データ座標）の中で曲線の極大/極小を探す。見つけた点の値と pt 座標を返す。"""
    pts = curve_data(curve, calx, caly)
    lo, hi = sorted(x_range)
    idx = [i for i, p in enumerate(pts) if lo <= p[0] <= hi]
    if not idx:
        raise CalibrationError(f"x={lo}〜{hi} に曲線の点が無い")
    # 点線・ノイズの粒に引っ張られないよう、曲線全体の幅の約 2% の窓で y を移動平均してから極値を探す
    # （吸着先の点は平滑化後の極値の位置。y の値は平滑化後のもの）
    half = max(1, len(pts) // 100)
    sm = []
    for i in range(len(pts)):
        win = pts[max(0, i - half): i + half + 1]
        sm.append(sum(p[1] for p in win) / len(win))
    sm_pt = []
    for i in range(len(pts)):
        win = pts[max(0, i - half): i + half + 1]
        sm_pt.append(sum(p[3] for p in win) / len(win))
    best = max(idx, key=lambda i: sm[i]) if kind == "max" else min(idx, key=lambda i: sm[i])
    # 単純な最大/最小だと区間の端に張り付くことがあるので、端に張り付いたら印を付ける
    at_edge = best in (idx[0], idx[-1])
    # 頂上の位置は「平滑化した曲線が頂上から 10% 下がるまでの連続区間」の重心で決める。
    # 線の太さ（約 1pt ≒ 数 °C）より細かい精度は図からは読めないので、ここでは方式を 1 つに固定する。
    # 実測（Taira 2026 Fig.1）: 鋭い破線のピーク 350.6 °C（本文 353）・粒の大きい点線 347.9 °C（本文 348）。
    # 生の極値や 2 次式の当てはめを曲線の様子で切り替える方式は、区間の取り方で結果が入れ替わり脆かった
    sign = 1 if kind == "max" else -1
    top = sign * sm[best]
    base = min(sign * v for v in sm)  # 曲線全体の底（狭い区間でも同じ基準になるように）
    thr = top - 0.10 * (top - base)
    idx_set = set(idx)
    l = r = best
    while l - 1 in idx_set and sign * sm[l - 1] >= thr:
        l -= 1
    while r + 1 in idx_set and sign * sm[r + 1] >= thr:
        r += 1
    reg = list(range(l, r + 1))
    w = [max(0.0, sign * pts[i][1] - thr) for i in reg]
    if sum(w) <= 0:
        return {"x": pts[best][0], "y": sm[best], "x_pt": pts[best][2], "y_pt": sm_pt[best], "at_edge": at_edge, "method": "smooth"}
    cx = sum(pts[i][0] * wi for i, wi in zip(reg, w)) / sum(w)
    # 印を置く点: 重心の x に最も近い点の x と、頂上の高さ（平滑化後）
    j = min(reg, key=lambda i: abs(pts[i][0] - cx))
    return {"x": cx, "y": sm[best], "x_pt": pts[j][2], "y_pt": sm_pt[best], "at_edge": at_edge, "method": "centroid"}


def label_value_mismatch(label: str, x_measured: float, curve: dict, calx: dict) -> str | None:
    """赤丸の札に書いた数値（本文の値）と、図から測った頂点の横軸の値が離れていれば、その旨の文を返す。

    Kawamoto 2017 Fig.1: 札は本文の「350 °C」、図の頂点は 333 °C（17 °C 差）で、黙って 350 と出していた。
    札の数値のうち、その曲線の横軸の範囲に入るものだけを比べる（"20 K毎分" のような別の量は除く）。
    許す差は 3 単位か横軸の幅の 2% の大きいほう（図から読める精度。Taira Fig.1 は ±2.5 °C で一致）。"""
    xs = [to_value(calx, q[0]) for q in curve.get("profile", [])]
    if not xs:
        return None
    lo, hi = min(xs), max(xs)
    tol = max(3.0, 0.02 * (hi - lo))
    for m in re.finditer(r"-?\d+(?:\.\d+)?", label):
        v = float(m.group())
        if lo <= v <= hi and abs(v - x_measured) > tol:
            return (f"札の値 {m.group()} と図から測った頂点 {x_measured:.0f} が {abs(v - x_measured):.0f} 離れている"
                    f"（本文の値が丸めか、別の測定の値かを確かめる）")
    return None


def share_axes(panels: dict, cals: dict, tol: float = 2.0) -> None:
    """軸を共有するパネルに、較正を貸し借りする。

    上下に積んだパネルは横軸の数字が一番下にしか書かれないことが多い（実測: Taira 2026 Fig.7・8・11）。
    左右端が tol pt 以内でそろうパネル同士は横軸が同じとみなし、較正の無いほうに写す。
    左右に並べたパネルの縦軸も、上下端がそろえば同じ扱いにする。較正は pt 座標なので写すだけでよい。
    """
    for pid, p in panels.items():
        cal = cals.setdefault(pid, {"errors": {}})
        fr = p["frame"]
        for axis, same in (("x", lambda a, b: abs(a[0] - b[0]) < tol and abs(a[2] - b[2]) < tol),
                           ("y_left", lambda a, b: abs(a[1] - b[1]) < tol and abs(a[3] - b[3]) < tol),
                           ("y_right", lambda a, b: abs(a[1] - b[1]) < tol and abs(a[3] - b[3]) < tol)):
            if axis in cal:
                continue
            for qid, q in panels.items():
                if qid != pid and axis in cals.get(qid, {}) and same(fr, q["frame"]):
                    cal[axis] = {**cals[qid][axis], "shared_from": qid}
                    cal["errors"].pop(axis, None)
                    break


# ---- 指示 → 描画の図形 -----------------------------------------------------

def _px(geom, scale):
    cx0, cy0 = geom["clip"][0], geom["clip"][1]
    return lambda x, y: [round((x - cx0) * scale, 1), round((y - cy0) * scale, 1)]


def _clusters(bbs: list, gap: float) -> list[list[float]]:
    """外接矩形を、隙間 gap 以内で近いもの同士まとめ、まとまりごとの外接矩形を返す。"""
    groups = [list(b) for b in bbs]
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]:
                    groups[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del groups[j]
                    merged = True
                    break
            if merged:
                break
    return groups


def _bbox_px(geom, scale, b):
    P = _px(geom, scale)
    (x0, y0), (x1, y1) = P(b[0], b[1]), P(b[2], b[3])
    return [x0, y0, x1, y1]


def resolve_figure(geom: dict, analysis: dict, scale: float) -> dict:
    """analysis（LLM の答え）を描画の図形に直す。各ステップに shapes と警告を付けて返す。"""
    P = _px(geom, scale)
    panels = {p["id"]: p for p in geom["panels"]}
    ans_panels = {p["id"]: p for p in analysis.get("panels", [])}
    cals = {pid: calibrate_panel(panels[pid], ap) for pid, ap in ans_panels.items() if pid in panels}
    share_axes(panels, cals)
    labels = {l["id"]: l for p in geom["panels"] for l in p["labels"]}
    labels.update({g["id"]: g for g in geom.get("regions", [])})  # 軸の無い図の部品（G 番号）も同じ扱い
    curves = {c["id"]: (p["id"], c) for p in geom["panels"] for c in p["curves"]}
    ticks = {t["id"]: t for p in geom["panels"] for t in p["ticks"] if t["id"]}

    def curve_axis(cid):
        pid, _ = curves[cid]
        info = (ans_panels.get(pid, {}).get("curves") or {}).get(cid) or {}
        return info.get("axis", "y_left")

    steps_out = []
    for si, st in enumerate(analysis.get("steps", [])):
        shapes, warns = [], []
        for act in st.get("actions", []):
            t = act.get("type")
            pid = act.get("panel") or (next(iter(panels)) if panels else None)
            try:
                if t in ("highlight_x_axis", "highlight_y_axis"):
                    # 軸: 目盛り数字の塊＋軸題のラベルを囲む
                    side = act.get("side") or ("bottom" if t == "highlight_x_axis" else "left")
                    p = panels[pid]
                    boxes = [tk["label_bbox"] for tk in p["ticks"] if tk["side"] == side and tk["label_bbox"]]
                    for lid in act.get("labels", []):
                        if lid in labels:
                            boxes.append(labels[lid]["bbox"])
                    fr = p["frame"]
                    if not boxes:
                        # 目盛り数字が無い軸（相対強度の縦軸など）: 軸の線のまわりだけを囲む
                        pad = 3.0
                        boxes = [{"bottom": [fr[0], fr[3] - pad, fr[2], fr[3] + pad], "top": [fr[0], fr[1] - pad, fr[2], fr[1] + pad],
                                  "left": [fr[0] - pad, fr[1], fr[0] + pad, fr[3]], "right": [fr[2] - pad, fr[1], fr[2] + pad, fr[3]]}[side]]
                    bb = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
                    # 軸の線そのものも光らせる
                    if side == "bottom":
                        line = [P(fr[0], fr[3]), P(fr[2], fr[3])]
                    elif side == "top":
                        line = [P(fr[0], fr[1]), P(fr[2], fr[1])]
                    elif side == "left":
                        line = [P(fr[0], fr[3]), P(fr[0], fr[1])]
                    else:
                        line = [P(fr[2], fr[3]), P(fr[2], fr[1])]
                    shapes.append({"kind": "axis", "box": _bbox_px(geom, scale, bb), "line": line, "label": act.get("label", "")})
                elif t == "band_x":
                    cal = cals[pid]["x"]
                    fr = panels[pid]["frame"]
                    x0, x1 = (to_pos(cal, v) for v in act["x_range"])
                    x0, x1 = sorted((x0, x1))
                    x0, x1 = max(x0, fr[0]), min(x1, fr[2])
                    shapes.append({"kind": "band", "box": _bbox_px(geom, scale, [x0, fr[1], x1, fr[3]]), "label": act.get("label", "")})
                elif t == "band_y":
                    cal = cals[pid][act.get("axis", "y_left")]
                    fr = panels[pid]["frame"]
                    y0, y1 = sorted(to_pos(cal, v) for v in act["y_range"])
                    shapes.append({"kind": "band", "box": _bbox_px(geom, scale, [fr[0], y0, fr[2], y1]), "label": act.get("label", "")})
                elif t == "circle_peak":
                    if act.get("peak"):
                        # K 番号で選ばれたとき: その候補の曲線と、候補の前後の狭い区間で精密化する
                        kid = act["peak"]
                        hit = [(cid_, c_, k_) for cid_, (_, c_) in curves.items() for k_ in c_.get("peaks", []) if k_["id"] == kid]
                        if not hit:
                            raise CalibrationError(f"ピーク候補 {kid} が無い")
                        cid, c, k = hit[0]
                        if act.get("curve") and act["curve"] != cid:
                            warns.append(f"{kid} は {cid} の候補（指示の curve={act['curve']} と食い違い。{cid} を採用）")
                    else:
                        cid = act["curve"]
                        k = None
                    cpid, c = curves[cid]
                    if "x" not in cals.get(cpid, {}) and k is not None:
                        # 横軸の較正が捨てられた図でも、K 候補は自分の位置を持つ。精密化せずその位置に赤丸を描く
                        # （Huang 2024 Fig.4: FTIR の目盛りが直線に乗らず、K 指定の赤丸 4 つが消えていた）
                        r = max(28.0, 0.035 * (panels[cpid]["frame"][2] - panels[cpid]["frame"][0]) * scale)
                        shapes.append({"kind": "circle", "center": P(k["x_pt"], k["y_pt"]), "r": round(r, 1),
                                       "label": act.get("label") or "", "color": c["color"], "approx": True})
                        continue   # 注意は出さない（approx に記録。見る人向けの情報ではない）
                    calx = cals[cpid]["x"]
                    caly = cals[cpid].get(curve_axis(cid))
                    if caly is None:
                        # 縦軸に数字が無い（相対強度など）: 紙面で上ほど大きいとみなして頂点だけを探す。
                        # 赤丸の位置には縦の値は要らない（表示する数値は本文の値を label に書かせている）
                        caly = {"a": -1.0, "b": 0.0, "log": False, "resid": 0.0, "assumed": True}
                    if k is not None:
                        # 候補の x の前後（曲線の幅の ±4%）を区間にする
                        xs = [p[0] for p in c["profile"]]
                        dx = 0.04 * (max(xs) - min(xs))
                        x_range = [to_value(calx, k["x_pt"] - dx), to_value(calx, k["x_pt"] + dx)]
                        # 紙面で上に凸＝値が大きい（軸が反転していなければ）。較正の傾きで判定する
                        up_is_max = caly["a"] < 0
                        extremum = ("max" if up_is_max else "min") if k["kind"] == "peak" else ("min" if up_is_max else "max")
                    else:
                        x_range, extremum = act["x_range"], act.get("extremum", "max")
                    ext = find_extremum(c, calx, caly, x_range, extremum)
                    for nm, cal_ in (("横軸", calx), ("縦軸", caly)):
                        if cal_.get("n", 3) < 3 and not cal_.get("assumed"):
                            warns.append(f"{cid} の{nm}の較正は目盛り 2 個だけ（対数軸かどうかを確かめられない）")
                    if k is not None:
                        xs_ = [q[0] for q in c["profile"]]
                        if abs(ext["x_pt"] - k["x_pt"]) > 0.02 * (max(xs_) - min(xs_)):
                            # 候補から離れた所に吸い寄せられた（隣の高いピークなど）: 赤丸は出すが警告する
                            warns.append(f"{k['id']} の精密化で候補から {abs(ext['x_pt'] - k['x_pt']):.1f}pt 動いた（隣のピークへの吸着を疑う）")
                    if ext["at_edge"]:
                        warns.append(f"{cid} の{extremum}が区間 {[round(v, 1) for v in x_range]} の端に張り付いた（区間の取り方を疑う）")
                    lab_warn = label_value_mismatch(act.get("label") or "", ext["x"], c, calx)
                    if lab_warn:
                        warns.append(f"{cid}: {lab_warn}")
                    r = max(28.0, 0.035 * (panels[cpid]["frame"][2] - panels[cpid]["frame"][0]) * scale)
                    shapes.append({"kind": "circle", "center": P(ext["x_pt"], ext["y_pt"]), "r": round(r, 1),
                                   "label": act.get("label") or f"{ext['x']:.0f}", "value": {"x": ext["x"], "y": ext["y"]},
                                   "color": c["color"]})
                elif t == "point":
                    # データ座標の 1 点（曲線に吸着しない注記）
                    calx = cals[pid]["x"]
                    caly = cals[pid][act.get("axis", "y_left")]
                    shapes.append({"kind": "circle", "center": P(to_pos(calx, act["x"]), to_pos(caly, act["y"])),
                                   "r": 26.0, "label": act.get("label", "")})
                elif t == "spotlight_curve":
                    cid = act["curve"]
                    _, c = curves[cid]
                    pts = [P(x, mid) for x, top, bot, mid in c["profile"]]
                    # 描画を軽くするため間引く（最大 300 点）
                    stride = max(1, len(pts) // 300)
                    shapes.append({"kind": "trace", "points": pts[::stride], "color": c["color"], "label": act.get("label", "")})
                elif t in ("box_label", "zoom_label"):
                    ids = act.get("labels") or [act.get("label_id")]
                    bbs = [labels[i]["bbox"] for i in ids if i in labels] + [ticks[i]["label_bbox"] for i in ids if i in ticks]
                    if not bbs:
                        raise CalibrationError(f"ラベル {ids} が無い")
                    if t == "zoom_label":
                        bb = [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
                        shapes.append({"kind": "zoom", "box": _bbox_px(geom, scale, bb), "label": act.get("label", "")})
                    else:
                        # 近いものだけをまとめて囲み、離れたものは別の枠にする
                        # （Jia 2017 Fig.4: 離れた 2 本の棒をまとめた枠が図の半分を覆った）
                        for bb in _clusters(bbs, gap=0.04 * (geom["clip"][2] - geom["clip"][0])):
                            shapes.append({"kind": "box", "box": _bbox_px(geom, scale, bb), "label": act.get("label", "")})
                elif t == "zoom_panel" and pid not in panels:
                    # パネルの無い図で zoom_panel が来たら、全体表示に戻す（寄りを解く）指示とみなす
                    W = (geom["clip"][2] - geom["clip"][0]) * scale
                    H = (geom["clip"][3] - geom["clip"][1]) * scale
                    shapes.append({"kind": "zoom", "box": [0, 0, round(W, 1), round(H, 1)]})
                elif t == "zoom_panel":
                    fr = panels[pid]["frame"]
                    shapes.append({"kind": "zoom", "box": _bbox_px(geom, scale, [fr[0] - 20, fr[1] - 10, fr[2] + 20, fr[3] + 20])})
                elif t == "box_norm":
                    # 退避路: 幾何の候補が無い図（ラスター・図式）では、図全体を 0〜1000 とした正規化座標の箱
                    b = act["box"]
                    W = (geom["clip"][2] - geom["clip"][0]) * scale
                    H = (geom["clip"][3] - geom["clip"][1]) * scale
                    shapes.append({"kind": act.get("as", "box"), "box": [b[0] * W / 1000, b[1] * H / 1000, b[2] * W / 1000, b[3] * H / 1000],
                                   "label": act.get("label", ""), "approx": True})
                elif t == "arrow_norm" and len([i for i in (act.get("labels") or []) if i in labels]) == 2:
                    # 部品から部品への矢印: 2 つの箱の中心を結ぶ線を、それぞれの箱の縁で切る
                    a, b = (labels[i]["bbox"] for i in act["labels"][:2])
                    ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
                    cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

                    def edge(bb, c, toward):
                        # 中心 c から toward へ向かう線が箱 bb の縁を横切る点
                        dx, dy = toward[0] - c[0], toward[1] - c[1]
                        ts = []
                        if dx:
                            ts += [((bb[2] if dx > 0 else bb[0]) - c[0]) / dx]
                        if dy:
                            ts += [((bb[3] if dy > 0 else bb[1]) - c[1]) / dy]
                        tt = min(t_ for t_ in ts if t_ > 0) if any(t_ > 0 for t_ in ts) else 0
                        return (c[0] + dx * tt, c[1] + dy * tt)

                    f_, t_ = edge(a, ca, cb), edge(b, cb, ca)
                    shapes.append({"kind": "arrow", "from": P(*f_), "to": P(*t_), "label": act.get("label", "")})
                elif t == "arrow_norm":
                    W = (geom["clip"][2] - geom["clip"][0]) * scale
                    H = (geom["clip"][3] - geom["clip"][1]) * scale
                    f, to = act["from"], act["to"]
                    shapes.append({"kind": "arrow", "from": [f[0] * W / 1000, f[1] * H / 1000], "to": [to[0] * W / 1000, to[1] * H / 1000],
                                   "label": act.get("label", ""), "approx": True})
                else:
                    warns.append(f"未知の指示 {t}")
            except (KeyError, CalibrationError, ZeroDivisionError) as e:
                warns.append(f"{t}: {e}")
        # 同じ警告が赤丸ごとに並ぶ（Huang 2024 Fig.2 で 3 回）ので、順序を保って重複を除く
        steps_out.append({"index": si, "shapes": shapes, "warnings": list(dict.fromkeys(warns))})
    return {"calibration": {pid: {k: v for k, v in c.items()} for pid, c in cals.items()}, "steps": steps_out}
