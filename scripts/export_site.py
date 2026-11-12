"""作った解説（work/*/player.html）を、研究室のメンバーだけが見るサイトとして Cloudflare Workers に載せる形に組み立てる。

    py -3.13 scripts/export_site.py [--out <フォルダ>]   # 組み立てて検査する（公開はしない）
    cd <出力先> ; npx wrangler deploy                     # 公開（必ず先に Access を掛ける）

見られる人は Cloudflare Access のメール許可で絞る（必ず Access を先に掛ける）。宛先は site.json（無ければ site.example.json を読む）。
出力はリポジトリの外（既定 ~/.local/share/pfe/cf_build）。同期するフォルダに置くと 1 GB 近くが流れる。
入れるのは解説の HTML・図・声・縮小画像・原文の画面（ページの JPEG。施主 2026-11-06「今の範囲のまま載せる」）。
解説の HTML には図と声を入れず、/a/（図）と /v/（図ごとの声の束）に分けて、開いている図の分だけ読む。
work/ の単体の player.html は変えない。組み立てには今の player/parts/ を使うので、player を直しても work/ を作り直さなくてよい。
解説の「Notion」のボタンは、ここで作る _redirects が論文のページへ転送する（施主 2026-11-06。開けるのは DB に権限のある人だけ）。
タグは棚の絞り込みと札に出す（施主 2026-11-06「ねっとのものにも」。編集は /make だけ）。
PDF 本体・作る機能は入れない。ページ画像を作るのに pymupdf が要る。
player の中の手元のパスは消してから配り、配る前に check() が全ファイルを検査する。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import base64
import hashlib
import html
import io
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "player"))
from build_player import _SLOT, _json_for_script, assemble_template  # noqa: E402
from pfe import tags as tagstore  # noqa: E402
from pfe.meta import is_public, notion_url, paper_meta, thumbnail  # noqa: E402
from pfe.shelf import known_journal, shelf_info  # noqa: E402
WORK = ROOT / "work"
OUT = Path.home() / ".local" / "share" / "pfe" / "cf_build"     # リポジトリの外（同期させない）
LIMIT = 25 * 1024 * 1024        # Workers Static Assets の 1 ファイル上限
MAX_FILES = 20000               # 1 版あたりのファイル数の上限
NO_TAG = "__none__"             # 棚の絞り込みの「未分類」（タグの名前と重ならない印）
_DATA = re.compile(r'data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+)[;,][A-Za-z0-9+/=,;:-]*')
_ASSET_REF = re.compile(r'/[av]/[0-9a-f]{16}\.[a-z]+')          # 解説の中の、別ファイルにした図と声への参照
# 図の WebP の選び方（全 803 図で実測し、崩れの大きい図を 2 倍で見て決めた）。
# 全体の PSNR や大きさの比では、余白の多い図の細い線の色のにじみ（赤と青の等高線が紫になる）を見逃すので、一番崩れた区画で見る
FIG_QUALITY = 85                # 非可逆にするときの品質
FIG_MIN_TILE_PSNR = 32          # 一番崩れた区画がこれ未満なら可逆
FIG_MIN_RATIO = 1.5             # 可逆が非可逆のこの倍率以内なら可逆（崩す得が小さい）
FIG_TILE = 48                   # 崩れを見る区画の一辺
FIG_RULE = f"q{FIG_QUALITY}t{FIG_MIN_TILE_PSNR}r{FIG_MIN_RATIO}"     # 基準を変えたら溜め置きを作り直す
# 配ってはいけない文字列（手元のパス・原本の名前）。data URL を除いた本文で探す（base64 の偶然の一致を避ける）
# ドライブ名は直前が英数字でないときだけ（JSON の中の「groups:\nAliphatic」の s:\ を拾った。2026-11-09）
_LEAK = re.compile(r'/mnt/[a-z]/|(?<![A-Za-z0-9])[A-Za-z]:\\|source\.pdf|Users[\\/]+[\w.-]+[\\/]|%PDF-')
ALLOWED = {".html", ".jpg", ".webp", ".txt", ".json"}
DOC_HTML = ROOT / "web" / "static" / "doc.html"
# 原文のページ画像の幅・形式・品質。WebP q70 は JPEG q75 の 66% で字の読みやすさは同じ（2 倍で比較）。縮小は小さいので JPEG のまま
PAGE_IMG = {240: ("jpg", 70), 1400: ("webp", 70)}
SITE_FILE = ROOT / "site.json" if (ROOT / "site.json").exists() else ROOT / "site.example.json"
SITE = json.loads(SITE_FILE.read_text(encoding="utf-8"))


def papers() -> list[dict]:
    """player.html があり、公開してよい（meta_override.json で非公開にしていない）論文を新しい順に並べる。"""
    out = []
    live = {t["id"]: t for t in tagstore.active(WORK)}      # 消したタグは出さない
    for d in sorted(WORK.iterdir()):
        player = d / "player.html"
        if not d.is_dir() or not player.is_file() or not is_public(d):
            continue
        bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8")) if (d / "bundle.json").exists() else {}
        if not bundle.get("figures"):
            continue        # 図の解説がまだ 1 つも無い本（登録だけ済んだもの）は棚に出さない
        meta = {**(bundle.get("paper") or {}), **paper_meta(d)}     # 人が直した題を優先
        figs = [f for f in json.loads((d / "figures.json").read_text(encoding="utf-8"))
                if (f["id"].startswith("fig") or f["id"].startswith("table") and f.get("clip"))] if (d / "figures.json").exists() else []
        out.append({"pid": d.name, "title": meta.get("title") or d.name,
                    "authors": ", ".join(a for a in (meta.get("authors") or []) if a),
                    "journal": meta.get("journal") or "", "year": meta.get("year") or "",
                    "doi": meta.get("doi") or "", "done": len(bundle.get("figures", [])), "figs": len(figs),
                    "mtime": player.stat().st_mtime, "src": player, "shelf": shelf_info(meta),
                    "biblio": (d / "biblio.json").is_file(), "thumb": thumbnail(d), "notion": notion_url(d),
                    "tags": [t["name"] for t in tagstore.visible(d, live)]})
    return sorted(out, key=lambda p: -p["mtime"])


def export_doc(d: Path, p: dict, public: Path, cache: Path) -> int:
    """原文の画面の材料（ページの JPEG と pages.json）を public/d/<pid>/ に置き、ページ数を返す。
    PDF が無い本（ほかの人から受け取った本など）は 0 を返す。そのときは原文のボタンが出ないだけ。
    画像は cache に作り置きし、PDF が新しくならない限り作り直さない。"""
    pdf = d / "source.pdf"
    if not pdf.is_file():
        return 0
    import pymupdf
    from pfe.pages import page_info
    info = {"pid": p["pid"], "title": p["title"], "player": f"/p/{p['pid']}.html",
            "notion": bool(p.get("notion")), **page_info(d)}      # 解説はこれを見て「Notion」のボタンを出す
    dest = public / "d" / p["pid"]
    dest.mkdir(parents=True)
    (cache / p["pid"]).mkdir(parents=True, exist_ok=True)
    doc = None
    for n in range(1, info["n_pages"] + 1):
        for w, (ext, quality) in PAGE_IMG.items():
            c = cache / p["pid"] / f"{n}_{w}.{ext}"
            if not c.exists() or c.stat().st_mtime < pdf.stat().st_mtime:
                doc = doc or pymupdf.open(pdf)
                page = doc[n - 1]
                zoom = w / page.rect.width
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                if ext == "jpg":
                    _write_atomic(c, pix.tobytes("jpeg", jpg_quality=quality))
                else:
                    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    _write_atomic(c, _encode_webp(im, quality=quality, method=4))
            shutil.copyfile(c, dest / c.name)
    (dest / "pages.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return info["n_pages"]


def _write_atomic(path: Path, data: bytes) -> None:
    """途中で止まっても書きかけの溜め置きを残さない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _encode_webp(im: Image.Image, **options) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "WEBP", **options)
    return buf.getvalue()


def _worst_tile_psnr(a: np.ndarray, b: np.ndarray) -> float:
    """2 枚の画像を区画に分け、一番崩れた区画の PSNR を返す（完全に同じなら 99）。"""
    tiles_y, tiles_x = max(1, a.shape[0] // FIG_TILE), max(1, a.shape[1] // FIG_TILE)
    h, w = min(a.shape[0], tiles_y * FIG_TILE), min(a.shape[1], tiles_x * FIG_TILE)
    err = ((a[:h, :w].astype(np.float32) - b[:h, :w].astype(np.float32)) ** 2).mean(axis=2)
    worst = max(float(row.mean()) for rows in np.array_split(err, tiles_y, axis=0) for row in np.array_split(rows, tiles_x, axis=1))
    return 99.0 if worst == 0 else 10 * float(np.log10(255 ** 2 / worst))


def fig_webp(data: bytes, cache: Path | None = None) -> bytes:
    """図を WebP にする。崩れが見える図と崩す得が小さい図は可逆、それ以外は q85。
    変換は 1 枚 1 秒ほどかかるので、元の中身と基準をキーに溜め置く（同じ結果になり、デプロイでも送り直さない）。"""
    path = cache / f"{hashlib.sha256(data).hexdigest()[:32]}_{FIG_RULE}.webp" if cache else None
    if path and path.exists():
        return path.read_bytes()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ValueError(f"図を読めない: {e}") from e
    if "A" in im.getbands() or "transparency" in im.info:      # 図のカードは白地なので、透明は白に重ねる
        rgba = im.convert("RGBA")
        rgb = Image.new("RGB", im.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
    else:
        rgb = im.convert("RGB")
    lossless = _encode_webp(rgb, lossless=True, method=4)
    lossy = _encode_webp(rgb, quality=FIG_QUALITY, method=4)
    use_lossy = len(lossless) > FIG_MIN_RATIO * len(lossy) and _worst_tile_psnr(
        np.asarray(rgb), np.asarray(Image.open(io.BytesIO(lossy)).convert("RGB"))) >= FIG_MIN_TILE_PSNR
    out = lossy if use_lossy else lossless
    if path:
        _write_atomic(path, out)
    return out


def _store(public: Path, folder: str, data: bytes, ext: str) -> str:
    """中身のハッシュを名前にして public/<folder>/ に置き、URL を返す。同じ中身は 1 つにまとまる。"""
    name = hashlib.sha256(data).hexdigest()[:16] + ext
    path = public / folder / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return f"/{folder}/{name}"


def split_assets(bundle: dict, public: Path, cache: Path | None = None) -> None:
    """bundle の図と声を別ファイルへ出し、bundle には URL だけを残す。図は WebP にする（大きさは変えないので注記はそのまま乗る）。
    声は図ごとに data URL の配列へ束ねる。1 本ずつだとファイル数の上限に早く当たり、
    Static Assets は Range を返さないので new Audio(url) では Safari で鳴らない恐れがある。"""
    for fig in bundle.get("figures", []) + [bundle[k] for k in ("overview", "conclusion") if bundle.get(k)]:
        img = fig.get("img") or ""
        if img.startswith("data:"):
            head, body = img.split(",", 1)
            if not head.startswith("data:image/"):
                raise ValueError(f"別ファイルにできない図の種類: {head}")
            fig["img"] = _store(public, "a", fig_webp(base64.b64decode(body), cache), ".webp")
        voices = [st.pop("audio", None) for st in fig.get("steps", [])]
        for v in voices:
            if v and not v.startswith("data:audio/"):
                raise ValueError(f"埋め込みでない声がある: {v[:60]}")
        if any(voices):
            fig["voicePack"] = _store(public, "v", json.dumps(voices).encode("utf-8"), ".json")


def site_player(text: str, public: Path, template: str, cache: Path | None = None) -> str:
    """単体の player.html から配る解説を作る。今のテンプレートに、手元のパスと図・声を抜いた bundle を入れる。"""
    m = _SLOT.search(text)
    if not m:
        raise ValueError("player の中に bundle の置き場が無い")
    b = json.loads(m.group(2))
    for k in ("source", "source_path_or_url", "title_extracted"):       # 論文の出どころ。表示には使っていない
        (b.get("paper") or {}).pop(k, None)
    split_assets(b, public, cache)
    payload = _json_for_script(b)
    # 関数を渡して、中身のバックスラッシュを re.sub に解釈させない
    return _SLOT.sub(lambda s: s.group(1) + payload + s.group(3), template, count=1)


def check(public: Path) -> list[str]:
    """組み立てた public/ を全部読み、配ってはいけない物が無いかを調べる。問題の一覧を返す（空なら合格）。"""
    bad = []
    files = [f for f in public.rglob("*") if f.is_file()]
    if len(files) > MAX_FILES:
        bad.append(f"ファイルが {len(files)} 個ある（上限 {MAX_FILES}）")
    for f in files:
        rel = f.relative_to(public).as_posix()
        if f.stat().st_size > LIMIT:
            bad.append(f"{rel}: 25 MiB を超える")
        if f.suffix.lower() not in ALLOWED and f.name not in ("_headers", "_redirects"):
            bad.append(f"{rel}: 配らない種類のファイル")
        if rel.startswith(("make/", "api/", ".wrangler/")):
            bad.append(f"{rel}: 配らない場所")
        if f.suffix.lower() == ".html":
            text = f.read_text(encoding="utf-8")
            for mime in {m.group(1).lower() for m in _DATA.finditer(text)}:
                if not mime.startswith(("image/", "audio/")):
                    bad.append(f"{rel}: 埋め込みの種類 {mime}")
            m = _LEAK.search(_DATA.sub("data:", text))
            if m:
                bad.append(f"{rel}: 手元のパスか原本らしき文字列 {m.group(0)!r}")
            slot = _SLOT.search(text) if rel.startswith("p/") else None
            if slot:
                if _DATA.search(slot.group(2)):
                    bad.append(f"{rel}: 図か声の埋め込みが残っている")
                for ref in set(_ASSET_REF.findall(slot.group(2))):
                    if not (public / ref[1:]).is_file():
                        bad.append(f"{rel}: 参照先のファイルが無い {ref}")
        if rel.startswith("v/") and f.suffix.lower() == ".json":
            voices = json.loads(f.read_text(encoding="utf-8"))
            if any(v is not None and not str(v).startswith("data:audio/") for v in voices):
                bad.append(f"{rel}: 声でないものが入っている")
    return bad


def redirects(items: list[dict]) -> str:
    """_redirects の中身。player は /make/... に原文と Notion があるかを聞くので、作り直さずに済むよう同じ URL を向ける。
    静的な転送を動的な転送（:pid）より先に書く（Cloudflare の指定。上限は静的 2,000・動的 100）。"""
    lines = ["/ /index.html 200", "/make /index.html 200"]      # html_handling:none ではフォルダ → index.html を解決しない
    lines += [f"/make/notion/{p['pid']} {p['notion']} 302" for p in items if p.get("notion")]
    lines += ["/make/doc/:pid /doc.html 200", "/make/api/papers/:pid/pages /d/:pid/pages.json 200"]
    return "\n".join(lines) + "\n"


def tag_chips(items: list[dict]) -> str:
    """棚の上のタグの絞り込み（手元の棚と同じく作った順）。本が 1 冊も無いタグは出さない。タグが無ければ何も出さない。"""
    e = html.escape
    count = {}
    for p in items:
        for t in p.get("tags") or []:
            count[t] = count.get(t, 0) + 1
    if not count:
        return ""
    chips = [("すべて", "", len(items))]
    chips += [(t["name"], t["name"], count[t["name"]]) for t in tagstore.active(WORK) if t["name"] in count]
    untagged = sum(not p.get("tags") for p in items)
    if untagged:
        chips.append(("未分類", NO_TAG, untagged))
    body = "".join(f'<button class="chip" data-tag="{e(key)}">{e(label)}<span class="c">{n}</span></button>' for label, key, n in chips)
    return f'<div class="chips" id="chips">{body}</div>'


def index_html(items: list[dict]) -> str:
    cards = []
    chips = tag_chips(items)
    for i, p in enumerate(items):        # items は登録の新しい順。i は並び替えの「登録が新しい順」に使う
        e = html.escape
        s = p["shelf"]
        first = s["first_author"]
        who = s["authors_line"]
        # 著者は姓で、先頭の人を太字に。正式名と全員の名前はマウスを乗せると出る
        who_html = f"<b>{e(first)}</b>{e(who[len(first):])}" if first and who.startswith(first) else e(who)
        tags = p.get("tags") or []
        pills = "".join(f'<span class="tagpill">{e(t)}</span>' for t in tags)
        tagrow = f'<div class="tagrow">{pills}</div>' if pills else ""
        tag_data = e(json.dumps(tags, ensure_ascii=False))
        # カード全体が <a> なので、中に <a> を入れない（ブラウザがカードを途中で切り、割れて縦に伸びた。2026-10-30 施主「本当にひどい」）
        when = datetime.fromtimestamp(p["mtime"]).strftime("%Y-%m-%d")
        img = f'<img src="/t/{e(p["pid"])}.jpg" alt="" loading="lazy">' if p["thumb"] else ""
        # 検索は画面に出ていない正式名・全員の名前・DOI でも当たる
        key = e(" ".join(str(x) for x in (p["title"], p["authors"], s["search"], s["year"], p["doi"], p["pid"], *tags)).lower())
        sort_data = f'data-i="{i}" data-year="{e(str(s["year"]))}" data-author="{e(first)}"'
        cards.append(f'''<a class="card" data-q="{key}" data-tags="{tag_data}" {sort_data} href="/p/{e(p["pid"])}.html">
  <div class="thumb">{img}{tagrow}</div>
  <div class="body"><div class="title">{e(p["title"])}</div>
  <div class="who" title="{e(p["authors"])}">{who_html}</div>
  <div class="jr" title="{e(s["journal"])}">{e(s["journal_short"])}</div>
  <div class="foot" title="登録 {when}"><span class="n">図 {p["done"]}{f" / {p['figs']}" if p["figs"] else ""}</span><span class="yr">{e(str(s["year"]))}</span></div></div>
</a>''')
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>論文の図の解説</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --sub:#5d6676; --line:#dfe3ea; --accent:#2457c5; --thumb:#eef1f5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#12151b; --card:#1b2029; --ink:#e6e9ef; --sub:#9aa3b2; --line:#2c3340; --accent:#7aa2ff; --thumb:#232933; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 "Hiragino Sans","Yu Gothic UI","Meiryo",system-ui,sans-serif; }}
main {{ max-width:1400px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .lead {{ color:var(--sub); margin:0 0 14px; font-size:14px; }}
.find {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 18px; }}
#q {{ flex:1 1 260px; max-width:420px; padding:9px 12px; border:1px solid var(--line); border-radius:10px;
      background:var(--card); color:var(--ink); font:inherit; font-size:14px; }}
#sort {{ padding:9px 10px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--ink); font:inherit; font-size:14px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, 280px); gap:16px; justify-content:start; }}
.card {{ display:flex; flex-direction:column; background:var(--card); border:1px solid var(--line); border-radius:18px;
        overflow:hidden; color:inherit; text-decoration:none; width:280px; height:340px; transition:border-color .15s, transform .15s; }}
@media (max-width:620px) {{ .grid {{ grid-template-columns:1fr; }} .card {{ width:auto; }} }}
.card:hover {{ border-color:var(--accent); transform:translateY(-2px); }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 18px; }}
.chip {{ border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:99px; padding:6px 14px; font:inherit; font-size:13px; cursor:pointer; min-height:34px; }}
.chip:hover {{ border-color:var(--accent); }}
.chip.on {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.chip .c {{ opacity:.7; margin-left:4px; font-size:12px; }}
.thumb {{ position:relative; height:170px; flex:none; background:var(--thumb); border-bottom:1px solid var(--line); }}
.tagrow {{ position:absolute; left:8px; right:8px; bottom:8px; display:flex; gap:4px; overflow:hidden; pointer-events:none; }}
.tagpill {{ font-size:11px; background:var(--accent); color:#fff; border-radius:99px; padding:1px 9px; white-space:nowrap; flex:none; }}
.thumb img {{ width:100%; height:100%; object-fit:contain; background:#fff; display:block; }}
.body {{ padding:12px 14px; display:flex; flex-direction:column; gap:4px; flex:1; min-height:0; overflow:hidden; }}
.title {{ font-weight:600; overflow-wrap:anywhere; line-height:1.45; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }}
.who,.jr {{ font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.who {{ color:var(--ink); }} .who b {{ font-weight:700; }}
.jr {{ color:var(--sub); font-style:italic; }}
.foot {{ display:flex; justify-content:space-between; align-items:baseline; margin-top:auto; padding-top:6px; font-size:12px; color:var(--sub); }}
.n {{ color:var(--accent); }}
.yr {{ font-size:15px; font-weight:700; color:var(--ink); }}
</style></head><body><main>
<h1>論文の図の解説</h1>
<p class="lead">図ごとに軸の意味と読みどころを、枠・赤丸・音声で解説します。カードを押すと再生画面になります。</p>
<div class="find"><input type="search" id="q" placeholder="題・著者・雑誌・年・タグで探す（{len(items)} 本）" autocomplete="off">
<select id="sort" title="並び順"><option value="added">登録が新しい順</option><option value="year_new">年が新しい順</option>
<option value="year_old">年が古い順</option><option value="author">著者名順（A→Z）</option></select></div>
{chips}<div class="grid" id="grid">{"".join(cards) or '<p class="lead">まだありません</p>'}</div>
<p class="lead" id="none" hidden>当てはまる論文がありません</p>
</main><script>
// タグの絞り込みと検索（空白で区切ると全部を含むものだけ）、解説から戻ったときのスクロール位置
const q = document.getElementById('q'), cards = [...document.querySelectorAll('.card')];
const chips = [...document.querySelectorAll('.chip')];
let sel = '';      // 選んでいるタグ（このブラウザに覚える。保存できない環境でも動く）
try {{ sel = localStorage.getItem('pfe.sitetag') || ''; }} catch (e) {{}}
if (!chips.some(c => c.dataset.tag === sel)) sel = '';      // 覚えていたタグが無くなっていたら「すべて」
function apply() {{
  const words = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
  let n = 0;
  for (const c of cards) {{
    const tags = JSON.parse(c.dataset.tags);
    const inTag = sel === '' || (sel === '{NO_TAG}' ? tags.length === 0 : tags.includes(sel));
    const on = inTag && words.every(w => c.dataset.q.includes(w));
    c.style.display = on ? '' : 'none'; n += on;
  }}
  chips.forEach(c => c.classList.toggle('on', c.dataset.tag === sel));
  document.getElementById('none').hidden = n > 0;
}}
q.oninput = apply;
chips.forEach(c => {{ c.onclick = () => {{
  sel = c.dataset.tag;
  try {{ localStorage.setItem('pfe.sitetag', sel); }} catch (e) {{}}
  apply();
}}; }});
// 並び順（施主 2026-11-09「著者名か年代順に並び替え」）。カードを並べ直すだけ。年・著者の無い本はどの順でも最後
const sortSel = document.getElementById('sort');
const yearOf = c => Number(c.dataset.year) || null, authorOf = c => c.dataset.author || '';
const byYear = dir => (a, b) => {{ const x = yearOf(a), y = yearOf(b); return x && y ? dir * (x - y) : !x - !y; }};
const byAuthor = (a, b) => {{ const x = authorOf(a), y = authorOf(b); return x && y ? x.localeCompare(y, 'en', {{sensitivity: 'base'}}) : !x - !y; }};
const byAdded = (a, b) => a.dataset.i - b.dataset.i;
const SORTS = {{added: [byAdded], year_new: [byYear(-1), byAuthor, byAdded], year_old: [byYear(1), byAuthor, byAdded], author: [byAuthor, byYear(1), byAdded]}};
function reorder() {{
  const cmp = SORTS[sortSel.value] || SORTS.added;
  document.getElementById('grid').append(...[...cards].sort((a, b) => {{ for (const f of cmp) {{ const r = f(a, b); if (r) return r; }} return 0; }}));
}}
try {{ sortSel.value = localStorage.getItem('pfe.sitesort') || 'added'; }} catch (e) {{}}
if (!sortSel.value) sortSel.value = 'added';      // 覚えていた値が今の選択肢に無い
sortSel.onchange = () => {{ try {{ localStorage.setItem('pfe.sitesort', sortSel.value); }} catch (e) {{}} reorder(); }};
reorder();
apply();
try {{
  const y = sessionStorage.getItem('pfe.scroll'); sessionStorage.removeItem('pfe.scroll');
  if (y !== null) window.scrollTo(0, +y);
  document.getElementById('grid').onclick = () => sessionStorage.setItem('pfe.scroll', String(window.scrollY));
}} catch (e) {{}}
</script></body></html>
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="見るだけのサイトを組み立てて検査する")
    ap.add_argument("--out", type=Path, default=OUT, help="出力先（G: の外にする）")
    out = ap.parse_args(argv).out
    items = papers()
    public = out / "public"
    if public.exists():
        shutil.rmtree(public)
    (public / "p").mkdir(parents=True)
    (public / "t").mkdir()
    (public / "d").mkdir()
    (public / "a").mkdir()
    (public / "v").mkdir()
    n_pages = 0
    for p in items:
        n_pages += export_doc(WORK / p["pid"], p, public, out / "page_cache")
    # 原文の画面。doc.html は web/static の写しで、ページの画像の場所だけ静的なファイルへ差し替える
    doc = DOC_HTML.read_text(encoding="utf-8")
    for old, new in (("${api}/page/${n}?w=240", "/d/${encodeURIComponent(pid)}/${n}_240.jpg"),
                     ("${api}/page/${n}?w=1400", "/d/${encodeURIComponent(pid)}/${n}_1400.webp"),
                     ('<a href="/make">', '<a href="/">')):
        if old not in doc:
            raise ValueError(f"doc.html に差し替える箇所が無い: {old}")     # 画面を直したらここも追従する
        doc = doc.replace(old, new)
    (public / "doc.html").write_text(doc, encoding="utf-8")
    template = assemble_template()
    for p in items:
        # 図と声は別ファイルへ。手元のパスと一緒に、写しからだけ抜く（work/ はそのまま）
        (public / "p" / f"{p['pid']}.html").write_text(site_player(p["src"].read_text(encoding="utf-8"), public, template, out / "fig_cache"), encoding="utf-8")
        if p["thumb"]:
            shutil.copyfile(p["thumb"], public / "t" / f"{p['pid']}.jpg")  # 一覧のカードの縮小画像（最初の図）
    (public / "index.html").write_text(index_html(items), encoding="utf-8")
    (public / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (public / "_headers").write_text("/*\n  X-Robots-Tag: noindex, nofollow\n", encoding="utf-8")
    (public / "_redirects").write_text(redirects(items), encoding="utf-8")
    cfg = {
        "name": SITE["worker"],
        "compatibility_date": SITE["compatibility_date"],
        "workers_dev": False,        # 公開 URL は独自ドメインの 1 本だけ
        "preview_urls": False,
        "routes": [{"pattern": SITE["domain"], "custom_domain": True}],
        "assets": {"directory": "public", "html_handling": "none", "not_found_handling": "none"},
    }
    (out / "wrangler.jsonc").write_text("// 生成物: scripts/export_site.py が作る。手で直さない。\n"
                                        + json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = [f for f in public.rglob("*") if f.is_file()]
    size = {k: sum(f.stat().st_size for f in (public / k).rglob("*") if f.is_file()) / 1e6 for k in "pavd"}
    print(f"{len(items)} 本・原文 {n_pages} ページ → {public}")
    print(f"解説の HTML {size['p']:.1f} MB・図 {size['a']:.1f} MB・声 {size['v']:.1f} MB・ページ画像 {size['d']:.1f} MB")
    print(f"ファイル {len(files)} 個（上限 {MAX_FILES}）・1 本の解説の HTML は最大 {max(f.stat().st_size for f in (public / 'p').iterdir()) / 1e3:.0f} KB")
    for p in items:
        print(f"  /p/{p['pid']}.html  図 {p['done']}/{p['figs']}  {p['title'][:60]}")
    # 棚の情報の抜け（公開は止めない）。新しい本は fill_biblio.py で埋まる
    print("Crossref で確かめていない本:", ", ".join(p["pid"] for p in items if not p["biblio"]) or "なし")
    print("年の無い本:", ", ".join(p["pid"] for p in items if not p["shelf"]["year"]) or "なし")
    print("略称の表に無い雑誌:", ", ".join(sorted({p["shelf"]["journal"] for p in items if p["shelf"]["journal"] and not known_journal(p["shelf"]["journal"])})) or "なし")
    bad = check(public)
    if bad:
        print(f"検査で {len(bad)} 件見つかった。公開しないこと:")
        for b in bad:
            print("  " + b)
        return 1
    print("検査: 合格（PDF 本体・手元のパスなし。全ファイルが上限内）")
    if SITE_FILE.name != "site.json":
        print("※ site.json が無いので例の宛先で組み立てた。デプロイの前に site.example.json を site.json に写して自分の値にする")
    print(f"公開先: https://{SITE['domain']}/  ※ 先に Access を掛ける")
    return 0


if __name__ == "__main__":
    sys.exit(main())
