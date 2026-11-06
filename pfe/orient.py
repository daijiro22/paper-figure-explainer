"""図表の向き: ページは縦のまま中身だけ回っている図表（Wang 2024 Table 2）を、正立させたページで扱う。

決まりごと:
- figures.json の page・clip・caption_bbox は常に**元のページの座標**。向きは図表ごとの rotate（無ければ 0）で持つ。
  原文の画面・本文の抽出は今までどおり元の座標だけを見ればよい。
- 切り出し画像と幾何（後段）は必ず figure_view() が返す page と clip の組で作る。resolve・player は
  「clip の原点を引いて倍率を掛ける」だけなので回転を知らなくてよい。
- rotate は page.show_pdf_page の rotate にそのまま渡す値。page.set_rotation では get_text の座標と向きが変わらず、
  新しいページに写すと文字が本当に横書きになる（Wang 2024 p6 で縦の行 126/129 → 3/129。2026-11-04 実測）。
- 回転の付いたページ（page.rotation != 0。Fergus 1969 の図版）は別の話なので触らない。
"""
from __future__ import annotations

import pymupdf

from .pdfutil import CAPTION_RE, overlap_size, page_lines

_VIEWS: dict[tuple, tuple] = {}      # (文書, ページ, 角度) → (元の文書, 正立の文書)。元を掴んでおき id の使い回しを防ぐ


def rotate_of_dir(dx: float, dy: float) -> int:
    """行の向き（get_text の dir）から、正立させる rotate を返す。斜めや横書きは 0。"""
    if dy < -0.9:
        return 270      # 下から上へ読む（Wang 2024 Table 2 は dir=(0,-1)）
    if dy > 0.9:
        return 90
    if dx < -0.9:
        return 180
    return 0


def rect_to_view(r, rotate: int, w: float, h: float) -> pymupdf.Rect:
    """元のページ（幅 w・高さ h）の矩形を、正立させたページの矩形に写す。"""
    x0, y0, x1, y1 = r
    if rotate == 270:
        return pymupdf.Rect(h - y1, x0, h - y0, x1)
    if rotate == 90:
        return pymupdf.Rect(y0, w - x1, y1, w - x0)
    if rotate == 180:
        return pymupdf.Rect(w - x1, h - y1, w - x0, h - y0)
    return pymupdf.Rect(x0, y0, x1, y1)


def rect_from_view(r, rotate: int, w: float, h: float) -> pymupdf.Rect:
    """rect_to_view の逆。w・h は元のページの幅と高さ。"""
    x0, y0, x1, y1 = r
    if rotate == 270:
        return pymupdf.Rect(y0, h - x1, y1, h - x0)
    if rotate == 90:
        return pymupdf.Rect(w - y1, x0, w - y0, x1)
    if rotate == 180:
        return pymupdf.Rect(w - x1, h - y1, w - x0, h - y0)
    return pymupdf.Rect(x0, y0, x1, y1)


def _raw_rotate(page, rotate: int) -> tuple[int, float, float]:
    """(回転情報を外したページに対する角度, その幅, 高さ)。rotate は見た目のページに対する角度。
    get_text と show_pdf_page は page.rotation を無視した座標で動く（Fergus 1969 の図版で実測・2026-11-04）。"""
    raw = page.rect * page.derotation_matrix
    return (rotate + page.rotation) % 360, abs(raw.width), abs(raw.height)


def page_to_view(page, r, rotate: int) -> pymupdf.Rect:
    """見た目のページの矩形（figures.json の座標）を、正立させたページの矩形に写す。"""
    rr, w, h = _raw_rotate(page, rotate)
    return rect_to_view(pymupdf.Rect(r) * page.derotation_matrix, rr, w, h)


def view_to_page(page, r, rotate: int) -> pymupdf.Rect:
    """page_to_view の逆。"""
    rr, w, h = _raw_rotate(page, rotate)
    return rect_from_view(r, rr, w, h) * page.rotation_matrix


def upright_page(doc, pno: int, rotate: int):
    """pno（1 始まり）を正立させた 1 ページの PDF のページ。同じページと角度なら作り直さない。"""
    key = (id(doc), pno, rotate)
    if key not in _VIEWS:
        src = doc[pno - 1]
        rr, w, h = _raw_rotate(src, rotate)
        base, base_no = doc, pno - 1
        if src.rotation:                     # 回転情報を外した写しを作る（付いたままだと show_pdf_page の枠がずれる）
            base = pymupdf.open()
            base.insert_pdf(doc, from_page=pno - 1, to_page=pno - 1)
            base[0].set_rotation(0)
            base_no = 0
        if rr == 0:
            view = base
        else:
            vw, vh = (h, w) if rr in (90, 270) else (w, h)
            view = pymupdf.open()
            vp = view.new_page(width=vw, height=vh)
            vp.show_pdf_page(vp.rect, base, base_no, rotate=rr)
        _VIEWS[key] = (doc, base, view)
    return _VIEWS[key][2][0]


def figure_view(doc, fig: dict):
    """図表を描く・読むときの (page, clip)。rotate が無ければ元のページと clip そのまま。"""
    rotate = fig.get("rotate") or 0
    src = doc[fig["page"] - 1]
    if not rotate:
        return src, pymupdf.Rect(fig["clip"])
    return upright_page(doc, fig["page"], rotate), page_to_view(src, fig["clip"], rotate)


def _drop_edge_fragment(view, clip, min_gap: float = 6.0, max_share: float = 0.15):
    """左右の端に、白い隙間で切り離された細い切れ端（隣の写真の端）があれば外す（Fergus 1969 Fig.13）。"""
    import numpy as np
    r = pymupdf.Rect(clip)
    pix = view.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=r, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    inked = (arr < 245).mean(axis=0) > 0.02       # 2% 以上に墨がある列
    runs, start = [], None
    for x, on in enumerate(inked):                # 墨のある列の連なり
        if on and start is None:
            start = x
        if not on and start is not None:
            runs.append([start, x])
            start = None
    if start is not None:
        runs.append([start, len(inked)])
    if len(runs) < 2:
        return clip
    sx = r.width / pix.width
    x0, x1 = r.x0, r.x1
    if runs[0][1] - runs[0][0] < max_share * pix.width and (runs[1][0] - runs[0][1]) * sx >= min_gap:
        x0 = r.x0 + runs[1][0] * sx - 1
    if runs[-1][1] - runs[-1][0] < max_share * pix.width and (runs[-1][0] - runs[-2][1]) * sx >= min_gap:
        x1 = r.x0 + runs[-2][1] * sx + 1
    return (x0, r.y0, x1, r.y1)


def _plate_limits(view, region):
    """スキャンの図版: ページ全面の下地の上に置かれた写真の範囲に、探す範囲を狭める（柱・ページ番号の墨を入れない）。"""
    area = view.rect.get_area()
    r = pymupdf.Rect(region)
    plates = [pymupdf.Rect(i["bbox"]) for i in view.get_image_info()]
    plates = [p for p in plates if p.get_area() < 0.9 * area and (p & r).get_area() > 0.2 * r.get_area()]
    if not plates:
        return region
    u = plates[0]
    for p in plates[1:]:
        u |= p
    u &= r
    return tuple(u)


def rotated_captions(doc, body: float) -> list[dict]:
    """向きの違うキャプションを、正立させたページで組み直して返す（bbox は正立側の座標・page は 1 に置き換えない元のページ）。

    行は page_lines で組むと向きが失われるので、向きは生の行から取り、キャプションの続きの行などは正立側の
    既存の処理（figures._find_captions）に任せる。本文の大きさ body は文書全体の値を渡す（1 ページから推定し直さない）。"""
    from . import figures as F
    out: list[dict] = []
    for pno, page in enumerate(doc, 1):
        raws: dict[int, list] = {}
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                # 行の向きは回転情報を無視した座標。見た目のページに対する角度に直す。
                # Fergus 1969 の図版は回転情報 270° で横倒しに見えるが、文字は外した座標で横書き → 90°
                rotate = (rotate_of_dir(*ln["dir"]) - page.rotation) % 360
                if rotate and CAPTION_RE.match("".join(s["text"] for s in ln["spans"])):
                    raws.setdefault(rotate, []).append(ln["bbox"])
        for rotate, boxes in raws.items():
            view = upright_page(doc, pno, rotate)
            rr, w, h = _raw_rotate(page, rotate)
            marks = [rect_to_view(b, rr, w, h) for b in boxes]
            lines = page_lines(view, 1)
            for cap in F._find_captions([view], [lines], body):
                # 正立側では、もともと横書きのキャプションが縦になるだけで、regex には合ってしまう。向きの違った行だけ採る
                if any(overlap_size(cap["bbox"], tuple(m))[0] > 3 and overlap_size(cap["bbox"], tuple(m))[1] > 3 for m in marks):
                    out.append(dict(cap, orig_page=pno, rotate=rotate, view_lines=lines))
    # 同じ番号が 2 度出たら最初のものを採る（横向きの表の続きのページ。_find_captions と同じ方針）
    return list({c["id"]: c for c in reversed(out)}.values())


def _trim_foreign_text(view, clip):
    """表の下に、正立側で縦になる行（もとは横書きの本文）が続くなら、その手前で切る。表の最後の文字で切ると
    文字より下にある絵が欠ける。find_tables の外枠がページの残りの本文まで広がることがある（Sikarwar 2016 Table 9・Alves 2023 Table 2）。"""
    across, along = [], []
    for b in view.get_text("dict", clip=pymupdf.Rect(clip))["blocks"]:
        for ln in b.get("lines", []):
            (along if abs(ln["dir"][1]) > 0.9 else across).append(ln["bbox"])
    if not across or not along:
        return clip
    last = max(b[3] for b in across)
    below = [b[1] for b in along if b[1] >= last - 2]
    if not below:
        return clip
    # 切ったあとの空白は墨の下端まで詰める
    from .figures import PAD, _ink_bbox
    bottom = min(clip[3], min(below) - 1)
    ink = _ink_bbox(view, (clip[0], clip[1], clip[2], bottom))
    return (clip[0], clip[1], clip[2], min(bottom, ink[3] + PAD) if ink else bottom)


def rotated_figures(doc, caps: list[dict], body: float, typ_w: float, bands, others: list[dict]) -> tuple[list[dict], list[dict]]:
    """向きの違うキャプションの図表の一覧（figures.json の形・元の座標）と、元のページで探索の境界にする塊を返す。

    元のページの柱とページ番号は正立側では左右に来るので、ink を取る範囲を元の帯の内側に狭める（上下の帯だけでは入る）。"""
    from . import figures as F
    out, blocks = [], []
    for cap in caps:
        pno, rotate = cap["orig_page"], cap["rotate"]
        src = doc[pno - 1]
        w, h = src.rect.width, src.rect.height
        view = upright_page(doc, pno, rotate)
        vw, vh = view.rect.width, view.rect.height
        safe = page_to_view(src, (0, bands[0], w, bands[1]), rotate)
        # 表: 短い題（"Table 1" だけの行）を左の段と見なさない。横向きの表はふつう幅いっぱいに置かれる（Kotake 2015 Table 1）。
        # 図: 題の段のまま（Fergus 1969 の図版は左右に 2 枚並ぶ）
        vcap = dict(cap, page=1, layout="full" if cap["kind"] == "table" else cap["layout"])
        # 同じページのほかの図表も、座標を正立側へ写して境界にする
        vothers = [dict(c, page=1) for c in caps if c is not cap and (c["orig_page"], c["rotate"]) == (pno, rotate)]
        vothers += [dict(o, page=1, bbox=list(page_to_view(src, o["bbox"], rotate))) for o in others if o["page"] == pno]
        paras = F.paragraph_lines(cap["view_lines"], body, vh, typ_w)
        clip, found = F._table_bbox(view, vcap) if cap["kind"] == "table" else None, "table"
        conf = "high"
        if clip is not None and clip[1] < cap["bbox"][3] < clip[3] and clip[1] >= cap["bbox"][1]:
            clip = (clip[0], cap["bbox"][3] + 0.5, clip[2], clip[3])     # 外枠の余白に題の下側が写り込まないように
        if clip is not None:
            clip = _trim_foreign_text(view, clip)
        if clip is None:
            for where in (["below", "above"] if cap["kind"] == "table" else ["above", "below"]):
                region = F._search_region(vcap, paras, vothers, vw, vh, where, (safe.y0, safe.y1))
                region = (max(region[0], safe.x0), region[1], min(region[2], safe.x1), region[3])
                if cap["kind"] != "table":
                    region = _plate_limits(view, region)
                u = F._ink_bbox(view, region)
                if u is not None and F._big_enough(u):
                    clip = (u[0] - F.PAD, u[1] - F.PAD, u[2] + F.PAD, u[3] + F.PAD)
                    if cap["kind"] != "table":
                        clip = _drop_edge_fragment(view, clip)
                    found = where
                    break
            # 見つからなければ「それらしい帯」を作らず、低信頼のままキャプションの周りを残して人に見せる
            conf = F._confidence(clip, paras) if clip is not None else "low"
            clip = clip or tuple(cap["bbox"])
        oclip = view_to_page(src, clip, rotate)
        ocap = view_to_page(src, cap["bbox"], rotate)
        out.append({
            "id": cap["id"], "number": cap["number"], "label": cap["label"], "kind": cap["kind"],
            "page": pno, "caption": F._join_caption(cap["chain"]),
            "caption_bbox": [round(v, 1) for v in ocap], "clip": [round(v, 1) for v in oclip],
            "layout": "full", "confidence": conf, "source": "auto", "found": found, "rotate": rotate,
        })
        blocks.append({"id": f"{cap['id']}-rotated", "kind": "table-body", "page": pno, "bbox": list(oclip)})
        blocks.append({"id": f"{cap['id']}-rotated-cap", "kind": "table-body", "page": pno, "bbox": list(ocap)})
    return out, blocks
