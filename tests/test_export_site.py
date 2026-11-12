"""見るだけのサイトの組み立て: 非公開・PDF・手元のパスが配られないことを確かめる。"""
import base64
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_site as ex  # noqa: E402


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


PNG_URL = "data:image/png;base64," + base64.b64encode(_png(Image.new("RGB", (8, 8), (10, 20, 30)))).decode("ascii")


def _player(source: str) -> str:
    # 実物と同じ形: bundle の置き場に、手元のパス入りの論文情報と画像の data URL が入っている
    b = {"paper": {"title": "題 </script>", "source": source, "source_path_or_url": source},
         "figures": [{"img": PNG_URL, "steps": [
             {"say": "一", "duration": 1.5, "audio": "data:audio/mpeg;base64,BBBB", "evidence": [{"source": "本文 p.2"}]},
             {"say": "声なし", "duration": 1.0}]}]}
    body = json.dumps(b, ensure_ascii=False).replace("</", "<\\/")
    return f'<html><script id="bundle" type="application/json">{body}</script></html>'


def _paper(work: Path, pid: str, public: bool = True) -> None:
    d = work / pid
    d.mkdir(parents=True)
    (d / "player.html").write_text(_player("/mnt/g/x/source.pdf"), encoding="utf-8")
    (d / "bundle.json").write_text('{"paper": {}, "figures": [{"id": "fig1"}]}', encoding="utf-8")
    import pymupdf
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(d / "source.pdf")
    if not public:
        (d / "meta_override.json").write_text('{"public": false}', encoding="utf-8")


_TEMPLATE = '<html><script id="bundle" type="application/json"></script></html>'


def test_site_player_removes_only_the_paths(tmp_path):
    out = ex.site_player(_player(r"G:\Mendeley\a.pdf"), tmp_path, _TEMPLATE)
    assert "Mendeley" not in out and "</script></html>" in out
    b = json.loads(ex._SLOT.search(out).group(2))
    assert b["paper"]["title"] == "題 </script>"
    assert b["figures"][0]["steps"][0]["evidence"][0]["source"] == "本文 p.2"   # 根拠の出どころは残す


def test_site_player_moves_figures_and_voices_out(tmp_path):
    """図は /a/ へ、声は図ごとの束にして /v/ へ。HTML には URL だけが残り、声のない段の並びも保つ。"""
    out = ex.site_player(_player("x"), tmp_path, _TEMPLATE)
    assert "base64" not in out
    fig = json.loads(ex._SLOT.search(out).group(2))["figures"][0]
    assert fig["img"].startswith("/a/") and fig["img"].endswith(".webp")
    with Image.open(tmp_path / fig["img"][1:]) as im:
        assert im.format == "WEBP" and im.size == (8, 8)                # 大きさを変えない（注記の座標がそのまま乗る）
    assert not any("audio" in st for st in fig["steps"])
    assert json.loads((tmp_path / fig["voicePack"][1:]).read_text(encoding="utf-8")) == ["data:audio/mpeg;base64,BBBB", None]
    assert [st["duration"] for st in fig["steps"]] == [1.5, 1.0]        # 時間割は声が届く前に決まる


def test_site_player_covers_overview_and_shares_same_content(tmp_path):
    b = {"paper": {}, "figures": [{"img": PNG_URL, "steps": []}],
         "overview": {"img": PNG_URL, "steps": [{"audio": "data:audio/mpeg;base64,BBBB"}]}}
    text = f'<script id="bundle" type="application/json">{json.dumps(b)}</script>'
    got = json.loads(ex._SLOT.search(ex.site_player(text, tmp_path, _TEMPLATE)).group(2))
    assert got["overview"]["img"] == got["figures"][0]["img"]           # 同じ中身は 1 ファイル
    assert len(list((tmp_path / "a").iterdir())) == 1
    assert got["overview"]["voicePack"].startswith("/v/") and "voicePack" not in got["figures"][0]


def test_site_player_stops_on_what_it_cannot_split(tmp_path):
    def text(b):
        return f'<script id="bundle" type="application/json">{json.dumps(b)}</script>'
    with pytest.raises(ValueError):
        ex.site_player(text({"figures": [{"img": "data:application/pdf;base64,AAAA"}]}), tmp_path, _TEMPLATE)
    with pytest.raises(ValueError):
        ex.site_player(text({"figures": [{"img": "data:image/png;base64,AAAA"}]}), tmp_path, _TEMPLATE)     # 壊れた図
    with pytest.raises(ValueError):
        ex.site_player(text({"figures": [{"steps": [{"audio": "audio/a.mp3"}]}]}), tmp_path, _TEMPLATE)
    with pytest.raises(ValueError):
        ex.site_player("<html></html>", tmp_path, _TEMPLATE)


def _thin_lines() -> Image.Image:
    """細い赤と青の線が交互に並ぶ図（等高線の形）。非可逆だと色がにじんで区別が弱くなる。"""
    rng = np.random.default_rng(0)
    a = np.full((400, 400, 3), 255, np.uint8)
    for y in range(0, 400, 4):
        xs = np.arange(400)
        ys = (y + (6 * np.sin(xs / (20 + y % 13)) + rng.integers(0, 2, 400)).astype(int)) % 400
        a[ys, xs] = (230, 30, 30) if y % 8 == 0 else (30, 30, 230)
    return Image.fromarray(a)


def _photo() -> Image.Image:
    """なだらかな濃淡に細かな粒がある図（電子顕微鏡の写真の形）。可逆だと大きいが、非可逆でも崩れが小さい。"""
    rng = np.random.default_rng(1)
    base = np.linspace(90, 170, 400)[None, :, None] + np.linspace(0, 40, 400)[:, None, None]
    return Image.fromarray(np.clip(base + rng.normal(0, 2.5, (400, 400, 3)), 0, 255).astype(np.uint8))


def _pixels(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def test_fig_webp_keeps_thin_coloured_lines_exact():
    src = _thin_lines()
    assert np.array_equal(_pixels(ex.fig_webp(_png(src))), np.asarray(src))


def test_fig_webp_compresses_photos_within_the_damage_limit():
    src = _photo()
    png = _png(src)
    out = ex.fig_webp(png)
    assert len(out) < len(png) / 3
    assert not np.array_equal(_pixels(out), np.asarray(src))            # 非可逆を選んでいる
    assert ex._worst_tile_psnr(np.asarray(src), _pixels(out)) >= ex.FIG_MIN_TILE_PSNR


def test_fig_webp_flattens_transparency_on_white():
    im = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    assert _pixels(ex.fig_webp(_png(im)))[0, 0].tolist() == [255, 255, 255]


def test_fig_webp_reuses_the_cache(tmp_path):
    png = _png(Image.new("RGB", (8, 8), "white"))
    first = ex.fig_webp(png, tmp_path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1 and ex.FIG_RULE in files[0].name             # 基準を変えると別のキーになる
    files[0].write_bytes(b"cached")
    assert ex.fig_webp(png, tmp_path) == b"cached" and first != b"cached"
    assert not list(tmp_path.glob("*.tmp"))


def test_worst_tile_finds_local_damage_that_a_global_measure_misses():
    a = np.full((480, 480, 3), 255, np.uint8)
    b = a.copy()
    assert ex._worst_tile_psnr(a, b) == 99.0
    b[0:48, 0:48] = 0                                                   # 100 区画のうち 1 つだけ壊す
    assert ex._worst_tile_psnr(a, b) < 1
    small = np.zeros((10, 10, 3), np.uint8)
    assert ex._worst_tile_psnr(small, small) == 99.0                    # 区画より小さい図でも動く


def test_shelf_card_shows_year_short_journal_and_family_names(tmp_path, monkeypatch):
    """施主 2026-11-06: 右下は登録日でなく論文の年、雑誌は略称、著者はわかりやすく。検索は隠れた形でも当たる。"""
    work = tmp_path / "work"
    _paper(work, "meng2018")
    (work / "meng2018" / "meta.json").write_text(json.dumps({
        "title": "The Nature of Hololignin", "doi": "10.1021/x", "journal": "ACS Sustainable Chemistry & Engineering", "year": 2018,
        "authors": ["Xianzhi Meng", "Yunqiao Pu", "Poulomi Sannigrahi", "Mi Li", "Shilin Cao", "Arthur J. Ragauskas"]}), encoding="utf-8")
    monkeypatch.setattr(ex, "WORK", work)
    out = tmp_path / "out"
    assert ex.main(["--out", str(out)]) == 0
    page = (out / "public" / "index.html").read_text(encoding="utf-8")
    assert '<span class="yr">2018</span>' in page
    assert ">ACS Sustainable Chem. Eng.</div>" in page
    assert "<b>Meng</b>, Pu, Sannigrahi … Ragauskas" in page
    key = page.split('data-q="')[1].split('"')[0]
    for word in ("acs sustainable chemistry", "acs sustainable chem eng", "xianzhi meng", "ragauskas", "2018", "10.1021/x"):
        assert word in key
    assert not re.search(r"<span>\d{4}-\d{2}-\d{2}</span>", page)       # 登録日はカードの表に出さない（title だけ）
    # 並び替え（施主 2026-11-09「著者名か年代順に並び替え」）: 選択肢と、カードに並べ直す鍵
    assert '<select id="sort"' in page and 'value="year_old"' in page and 'value="author"' in page
    assert 'data-i="0" data-year="2018" data-author="Meng"' in page


def test_build_skips_private_and_pdf(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _paper(work, "open1")
    _paper(work, "secret1", public=False)
    _paper(work, "empty1")      # 登録だけで解説がまだ無い本も出さない
    (work / "empty1" / "bundle.json").write_text('{"paper": {}, "figures": []}', encoding="utf-8")
    monkeypatch.setattr(ex, "WORK", work)
    out = tmp_path / "out"
    assert ex.main(["--out", str(out)]) == 0
    names = {f.relative_to(out / "public").as_posix() for f in (out / "public").rglob("*") if f.is_file()}
    assert "p/open1.html" in names
    assert any(n.startswith("a/") and n.endswith(".webp") for n in names)    # 図は別ファイルの WebP
    assert any(n.startswith("v/") and n.endswith(".json") for n in names)    # 声は図ごとの束
    assert not any(n.endswith(".png") for n in names)
    player = (out / "public" / "p" / "open1.html").read_text(encoding="utf-8")
    assert "base64" not in player and "voicePack" in player and "function loadVoice" in player    # 今のテンプレートで組み立てる
    assert {"d/open1/1_240.jpg", "d/open1/1_1400.webp", "d/open1/pages.json", "doc.html"} <= names   # 原文の画面
    doc = (out / "public" / "doc.html").read_text(encoding="utf-8")
    assert "/make/api" not in doc.split("fetch(")[0].split("figBoxes")[1]
    assert "_1400.webp" in doc and "_240.jpg" in doc                         # 画面が読む名前と置いた名前が同じ
    assert list((out / "fig_cache").glob("*.webp"))                          # 図の変換は溜め置く（public の外）
    assert not any("secret1" in n or "empty1" in n or n.endswith(".pdf") for n in names)
    assert "secret1" not in (out / "public" / "index.html").read_text(encoding="utf-8")
    cfg = (out / "wrangler.jsonc").read_text(encoding="utf-8")
    assert '"workers_dev": false' in cfg and '"preview_urls": false' in cfg


def test_notion_button_is_served_by_static_files(tmp_path, monkeypatch):
    """解説は /make/api/papers/<pid>/pages の notion を見てボタンを出し、/make/notion/<pid> へ飛ぶ。どちらも静的なファイルで答える。"""
    work = tmp_path / "work"
    url = "https://app.notion.com/p/abc123"
    _paper(work, "reg")
    (work / "reg" / "notion").mkdir()
    (work / "reg" / "notion" / "page.json").write_text(json.dumps({"page_id": "x", "url": url}), encoding="utf-8")
    _paper(work, "plain")       # Notion に登録の無い本
    _paper(work, "secret1", public=False)
    (work / "secret1" / "notion").mkdir()
    (work / "secret1" / "notion" / "page.json").write_text(json.dumps({"url": "https://app.notion.com/p/secret"}), encoding="utf-8")
    monkeypatch.setattr(ex, "WORK", work)
    out = tmp_path / "out"
    assert ex.main(["--out", str(out)]) == 0
    pub = out / "public"
    lines = (pub / "_redirects").read_text(encoding="utf-8").splitlines()
    assert f"/make/notion/reg {url} 302" in lines
    assert not any("plain" in x or "secret" in x for x in lines)                  # 非公開の本の URL を配らない
    # 静的な転送を動的な転送より先に書く（Cloudflare の指定）
    last_static = max(i for i, x in enumerate(lines) if ":pid" not in x)
    assert last_static < min(i for i, x in enumerate(lines) if ":pid" in x)
    assert "/make/api/papers/:pid/pages /d/:pid/pages.json 200" in lines          # 解説が聞く先は残す
    assert json.loads((pub / "d" / "reg" / "pages.json").read_text(encoding="utf-8"))["notion"] is True
    assert json.loads((pub / "d" / "plain" / "pages.json").read_text(encoding="utf-8"))["notion"] is False


def test_shelf_shows_tags_without_deleted_or_unused_ones(tmp_path, monkeypatch):
    from pfe import tags as T
    work = tmp_path / "work"
    for pid in ("a", "b", "c"):
        _paper(work, pid)
    used = T.create(work, "脱リグニン")
    other = T.create(work, 'He "quoted" <tag>')          # 記号が HTML を壊さない
    unused = T.create(work, "だれにも付いていないタグ")
    gone = T.create(work, "消したタグ")
    known = {t["id"] for t in (used, other, unused, gone)}
    T.set_paper_tags(work / "a", [used["id"], gone["id"]], known)
    T.set_paper_tags(work / "b", [used["id"], other["id"]], known)
    T.update(work, gone["id"], deleted=True)
    monkeypatch.setattr(ex, "WORK", work)
    out = tmp_path / "out"
    assert ex.main(["--out", str(out)]) == 0
    page = (out / "public" / "index.html").read_text(encoding="utf-8")
    assert '<button class="chip" data-tag="脱リグニン">脱リグニン<span class="c">2</span></button>' in page
    assert '<span class="tagpill">脱リグニン</span>' in page
    assert f'data-tag="{ex.NO_TAG}">未分類<span class="c">1</span>' in page      # タグの無い本が 1 冊ある
    assert '<span class="c">3</span>' in page                                     # すべて
    assert "消したタグ" not in page and "だれにも付いていないタグ" not in page
    assert '<tag>' not in page and "He &quot;quoted&quot; &lt;tag&gt;" in page
    assert "data-tags=\"[&quot;脱リグニン&quot;]\"" in page                       # 解説へのリンクの属性を壊さない


def test_shelf_has_no_chips_when_nothing_is_tagged(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _paper(work, "a")
    monkeypatch.setattr(ex, "WORK", work)
    out = tmp_path / "out"
    assert ex.main(["--out", str(out)]) == 0
    page = (out / "public" / "index.html").read_text(encoding="utf-8")
    assert 'id="chips"' not in page and 'data-tags="[]"' in page


def test_check_catches_what_must_not_ship(tmp_path):
    pub = tmp_path
    (pub / "p").mkdir()
    (pub / "a.pdf").write_bytes(b"x")
    (pub / "p" / "path.html").write_text(r'"source": "G:\\Mendeley\\a"', encoding="utf-8")
    (pub / "p" / "emb.html").write_text("data:application/pdf;base64,AAAA", encoding="utf-8")
    bad = " ".join(ex.check(pub))
    assert "a.pdf" in bad and "path.html" in bad and "emb.html" in bad


def _slot(bundle: dict) -> str:
    return f'<script id="bundle" type="application/json">{json.dumps(bundle)}</script>'


def test_check_catches_broken_split(tmp_path):
    """図や声の埋め込みが残る・参照先が無い・声の束に別の物が入る、を配る前に止める。"""
    (tmp_path / "p").mkdir()
    (tmp_path / "v").mkdir()
    (tmp_path / "p" / "left.html").write_text(_slot({"figures": [{"img": "data:image/png;base64,AAAA"}]}), encoding="utf-8")
    (tmp_path / "p" / "gone.html").write_text(_slot({"figures": [{"img": "/a/0123456789abcdef.webp"}]}), encoding="utf-8")
    (tmp_path / "v" / "bad.json").write_text('["data:image/png;base64,AAAA", null]', encoding="utf-8")
    bad = " ".join(ex.check(tmp_path))
    assert "left.html" in bad and "gone.html" in bad and "0123456789abcdef" in bad and "v/bad.json" in bad


def test_check_accepts_split_player(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "v").mkdir()
    (tmp_path / "a" / "0123456789abcdef.webp").write_bytes(b"x")
    (tmp_path / "v" / "fedcba9876543210.json").write_text('["data:audio/mpeg;base64,AAAA", null]', encoding="utf-8")
    fig = {"img": "/a/0123456789abcdef.webp", "voicePack": "/v/fedcba9876543210.json"}
    (tmp_path / "p" / "ok.html").write_text(_slot({"figures": [fig]}), encoding="utf-8")
    assert ex.check(tmp_path) == []


def test_check_ignores_base64_lookalikes(tmp_path):
    # 画像の base64 の中に偶然 /mnt/g/ が現れても誤検知しない
    (tmp_path / "ok.html").write_text("data:image/png;base64,AA/mnt/g/AA", encoding="utf-8")
    assert ex.check(tmp_path) == []


def test_check_ignores_colon_before_escaped_newline_but_catches_drives(tmp_path):
    # 表の引用「Functional groups:」＋改行は JSON で groups:\n になる。s:\ をドライブ名と見ない（balakshin2020 で誤検知）
    (tmp_path / "ok.html").write_text(json.dumps({"quote": "Functional groups:\nAliphatic OH"}), encoding="utf-8")
    assert ex.check(tmp_path) == []
    # 直前が英数字でなければ、今まで通りドライブ名として止める
    for i, s in enumerate([r'"C:\Users\x', r'(G:\\001', r' D:\tmp']):
        (tmp_path / f"bad{i}.html").write_text(s, encoding="utf-8")
    bad = " ".join(ex.check(tmp_path))
    assert "bad0.html" in bad and "bad1.html" in bad and "bad2.html" in bad
