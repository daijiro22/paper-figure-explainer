"""中身が回っている図表（pfe/orient.py）。前半は人工の PDF、後半は Mendeley の実物（無ければ飛ばす）。"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pfe import figures as F  # noqa: E402
from pfe.orient import rect_from_view, rect_to_view, rotate_of_dir, rotated_captions, upright_page  # noqa: E402
from pfe.tables import table_geometry  # noqa: E402
from _paths import local  # noqa: E402


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_rect_round_trip(rotate):
    r = (10, 20, 110, 70)
    assert tuple(rect_from_view(rect_to_view(r, rotate, 400, 600), rotate, 400, 600)) == pytest.approx(r)


@pytest.mark.parametrize("d, rot", [((1, 0), 0), ((0, -1), 270), ((0, 1), 90), ((-1, 0), 180), ((0.7, 0.7), 0)])
def test_rotate_of_dir(d, rot):
    assert rotate_of_dir(*d) == rot


def _paper(angle: int):
    """本文は横書き、1 ページ目の表の題だけ angle 度回した PDF。"""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    for i in range(30):
        page.insert_text((30, 40 + i * 12), "Body text of the paper continues across the column here.", fontsize=10)
    page2 = doc.new_page(width=400, height=600)
    page2.insert_text((200, 300), "Table 1. Rotated caption text", fontsize=8, rotate=angle)
    return doc


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_any_angle_becomes_horizontal(angle):
    """どの角度でも、正立側で題が左から右へ読める向きになる（回す向きの取り違えを防ぐ）。"""
    doc = _paper(angle)
    caps = rotated_captions(doc, 10.0)
    assert [c["id"] for c in caps] == ["table1"] and caps[0]["orig_page"] == 2
    view = upright_page(doc, 2, caps[0]["rotate"])
    dirs = [ln["dir"] for b in view.get_text("dict")["blocks"] for ln in b.get("lines", [])]
    assert dirs and all(d[0] == pytest.approx(1, abs=0.01) for d in dirs)
    # 正立側で見つけた題の位置を元の座標へ戻すと、元のページの題に重なる
    src = doc[1]
    box = rect_from_view(caps[0]["bbox"], caps[0]["rotate"], src.rect.width, src.rect.height)
    raw = next(ln["bbox"] for b in src.get_text("dict")["blocks"] for ln in b.get("lines", []))
    assert (box & pymupdf.Rect(raw)).get_area() > 0.8 * pymupdf.Rect(raw).get_area()


def test_horizontal_caption_is_not_rotated():
    assert rotated_captions(_paper(0), 10.0) == []


# --- 実物 ------------------------------------------------------------------------

_MEN = {
    "wang": r"Wang et al\Wang et al. - 2024 - Thermogravimetric*.pdf",
    "kotake": r"Kotake, Kawamoto, Saka\Kotake, Kawamoto, Saka - 2015 - Pyrolytic*.pdf",
    "alves": r"Alves et al\Alves et al. - 2023*.pdf",
    "sikarwar": r"Sikarwar et al\Sikarwar et al. - 2016*.pdf",
    "yao": r"Yao, Minami, Kawamoto\Yao, Minami, Kawamoto - 2024 - Energy Advances*.pdf",
    "fergus": r"Fergus et al\Fergus et al. - 1969*.pdf",
    "jakab": r"Jakab, Faix, Till\Jakab, Faix, Till - 1997*.pdf",
}


def _figs(key):
    paths = sorted(glob.glob(local(_MEN[key])))
    if not paths:
        pytest.skip(f"{key} の PDF が無い")
    doc = pymupdf.open(paths[0])
    return doc, {f["id"]: f for f in F.detect_figures(doc)}


def test_wang_table_whole_and_readable():
    """Wang 2024 Table 2: 全列・全行を取り、見出しを題に混ぜず、種名と数値の並びが正しい。枠は元の座標で持つ。"""
    doc, figs = _figs("wang")
    t = figs["table2"]
    assert t["rotate"] == 270 and t["found"] == "table" and t["confidence"] == "high" and t["page"] == 6
    assert t["caption"].endswith("wood carbonization")
    assert t["clip"][3] - t["clip"][1] > t["clip"][2] - t["clip"][0]     # 元のページでは縦長
    page, clip = F.figure_view(doc, t)
    assert clip.width > clip.height                                       # 正立側では横長
    text = table_geometry(page, clip)["table_text"]
    assert text.splitlines()[0].startswith("Classification Species")
    assert "Japanese cedar 360.9 315.6 336.7 21.1 82.7 70.8 0.97 80.6 83.4" in text
    assert "Paulownia 357.7 285.4 343.7 58.3 87.9 57.1 0.79 69.8 88.4" in text
    assert "Basic parameters" not in text                                 # 題の行を枠に入れない


def test_render_matches_view(tmp_path):
    """切り出し画像は正立側の枠と同じ縦横比（注記の位置が画像と合う）。"""
    from PIL import Image
    doc, figs = _figs("wang")
    scale = F.render_figure(doc, figs["table2"], tmp_path / "t.png", long_edge=900)
    _, clip = F.figure_view(doc, figs["table2"])
    w, h = Image.open(tmp_path / "t.png").size
    assert w == pytest.approx(clip.width * scale, abs=2) and h == pytest.approx(clip.height * scale, abs=2)


def test_mixed_page_only_table_rotated():
    """Kotake 2015 p6: 横書きの本文と横向きの表が同じページ。表だけ回し、短い題でも幅いっぱいに取る。"""
    doc, figs = _figs("kotake")
    assert [f["id"] for f in figs.values() if f.get("rotate")] == ["table1"]
    _, clip = F.figure_view(doc, figs["table1"])
    assert clip.width > 600 and clip.y0 > 283      # 本文（正立側の y<284）を入れない


def test_continued_pages_counted_once():
    """Alves 2023: "Table 2 (Contd.)" を別の表にしない。続きのページは今は取らない（監査の型 6）。"""
    doc, figs = _figs("alves")
    assert figs["table2"]["page"] == 7 and figs["table3"]["page"] == 9 and figs["table4"]["page"] == 11


def test_trailing_body_text_excluded():
    """Sikarwar 2016 Table 9: find_tables の外枠が広がっても、表の下の本文（正立側で縦）を入れない。"""
    doc, figs = _figs("sikarwar")
    _, clip = F.figure_view(doc, figs["table9"])
    assert clip.y1 < 300, clip


def test_plates_on_page_with_rotation_flag():
    """Fergus 1969: 回転情報 270° で横倒しに見える図版。左右に並ぶ 2 枚を別々に、文字が読める向きで取る。
    旧: 文字の座標（回転情報なし）と画像の座標（あり）が食い違い、別の場所を切っていた（Fig.6 と 7 が重なった）。"""
    doc, figs = _figs("fergus")
    for fid in ("fig6", "fig7", "fig8", "fig11", "fig13", "fig14", "fig15"):
        assert figs[fid]["rotate"] == 90, fid
    assert not figs["fig1"].get("rotate")                          # 回転情報の無いページは今までどおり
    page, c6 = F.figure_view(doc, figs["fig6"])
    _, c7 = F.figure_view(doc, figs["fig7"])
    assert page.rect.width > page.rect.height                       # 正立側は横長のページ
    assert c6.x1 < c7.x0 and c6.width > 200 and c7.width > 200      # 2 枚が重ならない
    dirs = {tuple(round(x) for x in ln["dir"]) for b in page.get_text("dict")["blocks"] for ln in b.get("lines", [])}
    assert dirs == {(1, 0)}
    # 元の座標へ戻せる（原文の画面の枠は横倒しの見た目のページの中）
    r = doc[6].rect
    x0, y0, x1, y1 = figs["fig6"]["clip"]
    assert 0 <= x0 < x1 <= r.width and 0 <= y0 < y1 <= r.height
    _, c13 = F.figure_view(doc, figs["fig13"])
    assert c13.x0 > 370, c13                                        # 隣の写真（Fig.12）の切れ端を入れない


def test_scanned_table_with_rotation_flag():
    """Jakab 1997 p6: 回転情報つきのスキャンの表も正立させ、表の中身を読める。"""
    doc, figs = _figs("jakab")
    t = figs["table2"]
    assert t["rotate"] == 90 and t["found"] == "table"
    page, clip = F.figure_view(doc, t)
    assert clip.width > clip.height
    assert table_geometry(page, clip)["table_text"].startswith("No. Lignin Char")     # 中身は OCR なので字は崩れる


def test_margin_watermark_does_not_rotate():
    """RSC の余白の縦書き（Open Access の表示）では回さない。"""
    doc, figs = _figs("yao")
    assert not [f["id"] for f in figs.values() if f.get("rotate")]
