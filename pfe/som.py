"""Set-of-Mark 画像: 図の PNG に、幾何解析で見つけた候補の番号（T/C/L）を描き込む。

LLM はこの画像を見て「T3 は 200」「C1 は Wood の DTG」と番号で答える。座標は答えさせない。
番号札は対象に重ならないよう外側に寄せ、色は候補の種類ごとに変える。
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _font(size: int):
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def to_px(clip, scale):
    """PDF 座標 → 図 PNG の画素座標の変換関数を返す。"""
    cx0, cy0 = clip[0], clip[1]
    return lambda x, y: ((x - cx0) * scale, (y - cy0) * scale)


def _tag(draw, xy, text, fill, font, anchor="mm"):
    """白抜きの小さな番号札を描く。"""
    x, y = xy
    l, t, r, b = draw.textbbox((x, y), text, font=font, anchor=anchor)
    pad = 3
    draw.rectangle([l - pad, t - pad, r + pad, b + pad], fill=fill)
    draw.text((x, y), text, fill="white", font=font, anchor=anchor)


def render_som(fig_png: str, geom: dict, scale: float, out_png: str) -> None:
    """fig_png に番号札を重ねて out_png に保存する。"""
    im = Image.open(fig_png).convert("RGB")
    # 札を外に出す余白を足す
    m = 60
    canvas = Image.new("RGB", (im.width + 2 * m, im.height + 2 * m), "white")
    canvas.paste(im, (m, m))
    draw = ImageDraw.Draw(canvas)
    f = _font(max(16, int(im.width / 70)))
    tp = to_px(geom["clip"], scale)

    def P(x, y):
        px, py = tp(x, y)
        return px + m, py + m

    for p in geom["panels"]:
        fx0, fy0 = P(p["frame"][0], p["frame"][1])
        fx1, fy1 = P(p["frame"][2], p["frame"][3])
        draw.rectangle([fx0, fy0, fx1, fy1], outline=(120, 120, 120), width=1)
        _tag(draw, (fx0 + 4, fy0 - 14), p["id"], (90, 90, 90), f, anchor="lm")
        # 目盛り数字: 青の枠＋外側に札
        for t in p["ticks"]:
            if not t["label_bbox"]:
                continue
            x0, y0 = P(t["label_bbox"][0], t["label_bbox"][1])
            x1, y1 = P(t["label_bbox"][2], t["label_bbox"][3])
            draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(30, 90, 220), width=2)
            if t["side"] == "left":
                _tag(draw, (x0 - 8, (y0 + y1) / 2), t["id"], (30, 90, 220), f, anchor="rm")
            elif t["side"] == "right":
                _tag(draw, (x1 + 8, (y0 + y1) / 2), t["id"], (30, 90, 220), f, anchor="lm")
            elif t["side"] == "bottom":
                _tag(draw, ((x0 + x1) / 2, y1 + 16), t["id"], (30, 90, 220), f, anchor="mm")
            else:
                _tag(draw, ((x0 + x1) / 2, y0 - 16), t["id"], (30, 90, 220), f, anchor="mm")
        # 曲線: 曲線の右端付近に、その曲線の色で札
        for c in p["curves"]:
            prof = c["profile"]
            if not prof:
                continue
            # 札の位置は曲線の 85% 地点（右端は軸と重なりやすい）
            k = prof[int(len(prof) * 0.85)]
            x, y = P(k[0], k[3])
            col = tuple(int(c["color"][i:i + 2], 16) for i in (1, 3, 5))
            draw.line([x, y, x + 18, y - 22], fill=col, width=2)
            _tag(draw, (x + 20, y - 26), c["id"], col, f, anchor="lm")
            # ピーク・谷の候補: 小さな菱形＋K 番号（ピークは上に、谷は下に札）
            for k in c.get("peaks", []):
                kx, ky = P(k["x_pt"], k["y_pt"])
                d = 7
                draw.polygon([(kx, ky - d), (kx + d, ky), (kx, ky + d), (kx - d, ky)], outline=(0, 0, 0), fill=col)
                dy = -24 if k["kind"] == "peak" else 24
                _tag(draw, (kx, ky + dy), k["id"], (20, 20, 20), f, anchor="mm")
        # 図中ラベル: 灰色の点線枠＋左上に札
        for l in p["labels"]:
            x0, y0 = P(l["bbox"][0], l["bbox"][1])
            x1, y1 = P(l["bbox"][2], l["bbox"][3])
            draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(200, 40, 160), width=1)
            _tag(draw, (x0 - 2, y0 - 12), l["id"], (200, 40, 160), f, anchor="lm")
    # 画像の図: 縁に 0〜1000 の目盛りと薄い格子を描く（box_norm で答えるときの目安。目測のずれを減らす）
    if geom.get("kind") == "raster":
        fg = _font(max(14, int(im.width / 110)))
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for v in range(0, 1001, 100):
            gx, gy = m + im.width * v / 1000, m + im.height * v / 1000
            ld.line([gx, m, gx, m + im.height], fill=(0, 150, 255, 45), width=1)
            ld.line([m, gy, m + im.width, gy], fill=(0, 150, 255, 45), width=1)
            ld.text((gx, m - 8), str(v), fill=(0, 110, 220, 255), font=fg, anchor="md")
            ld.text((m - 6, gy), str(v), fill=(0, 110, 220, 255), font=fg, anchor="rm")
        canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
        draw = ImageDraw.Draw(canvas)
    # 軸の無い図の部品（G 番号）: 緑の枠＋左上に札。画像から切り出した候補は数が多いので細く・小さく
    fs = _font(max(13, int(im.width / 100)))
    for g in geom.get("regions", []):
        x0, y0 = P(g["bbox"][0], g["bbox"][1])
        x1, y1 = P(g["bbox"][2], g["bbox"][3])
        small = g.get("src") in ("raster", "table")
        draw.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=(0, 140, 70), width=1 if small else 2)
        _tag(draw, (x0 - 3, y0 - (10 if small else 14)), g["id"], (0, 140, 70), fs if small else f, anchor="lm")
    canvas.save(out_png)
