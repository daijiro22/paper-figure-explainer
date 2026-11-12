from __future__ import annotations
"""bundle.json から単一 HTML の player を作る。

使い方:
    py -3.13 player/build_player.py work/<id>/bundle.json work/<id>/player.html
    py -3.13 player/build_player.py --template-only out.html   # bundle を入れない（開くと bundle.json を置く画面が出る）

- 図の画像・音声が相対パスなら bundle.json のあるフォルダ基準で読み、data URL として埋め込む（embed_assets=True）。
- テンプレートは player/parts/ の部品から組み立てる（assemble_template）。shell.html の /*@include 名前*/ の行を、
  その部品の中身に置き換えるだけ。部品はすべて 1 つの <script> の中でつながり、shell.html の IIFE を共有する:
    shell.html   HTML の骨組み（DOM の部品・bundle の置き場・IIFE の開閉）
    player.css   見た目
    timeline.js  DOM に触れない部分（定数・寄り・時間割・stateAt・window.__player）。node でも動く
    stage.js     DOM 側で共有する変数（$・S・Q・CAPTURE など）と舞台の縮尺
    overlay.js   図の上の SVG の注記（帯・赤丸・なぞり・軸・枠・矢印）
    panel.js     状態を画面に反映（図の切り替え・根拠パネル・字幕・POINT・まとめ・renderAt・exportMode）
    controls.js  音声・再生制御・初期化・操作部品のイベント
  読み込み時に実行される文があるので、部品の順番は変えない（shell.html の並びのまま）。
- 組み立てた <script id="bundle" type="application/json"> の中身を bundle に差し替える。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import base64
import json
import mimetypes
import re
from pathlib import Path

PARTS = Path(__file__).with_name("parts")
TEMPLATE = PARTS / "shell.html"

# 骨組みの中で部品を差し込む行。行全体（改行まで）を部品の中身に置き換える
_INCLUDE = re.compile(r"^/\*@include ([\w.\-]+)\*/\n", re.MULTILINE)
# 部品に書くと、HTML に埋め込んだときに <script> や <style> が途中で閉じてしまう文字列
_FORBIDDEN = {".js": ("</script", "<!--"), ".css": ("</style", "<!--")}

# テンプレート中の bundle 置き場（中身は空）。DOTALL で中身ごと置き換える
_SLOT = re.compile(r'(<script id="bundle" type="application/json">)(.*?)(</script>)', re.DOTALL)

# mimetypes が環境で未登録のことがあるので、よく使う拡張子は固定しておく
_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
}


def _is_inline_or_remote(s: str) -> bool:
    """すでに data URL か、http(s) の URL なら埋め込み対象外"""
    low = s.lower()
    return low.startswith(("data:", "http://", "https://", "blob:"))


def _to_data_url(rel: str, base: Path) -> str:
    """相対パスのファイルを data URL にする。見つからなければ例外（黙って欠落させない）"""
    path = (base / rel).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"埋め込む資産が見つからない: {rel}（{path}）")
    mime = _MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _embed(bundle: dict, base: Path) -> int:
    """figures[].img と steps[].audio を data URL に置き換える。置換した数を返す"""
    n = 0
    for fig in bundle.get("figures", []) + [bundle[k] for k in ("overview", "conclusion") if bundle.get(k)]:
        img = fig.get("img")
        if isinstance(img, str) and img and not _is_inline_or_remote(img):
            fig["img"] = _to_data_url(img, base)
            n += 1
        for step in fig.get("steps", []):
            au = step.get("audio")
            if isinstance(au, str) and au and not _is_inline_or_remote(au):
                step["audio"] = _to_data_url(au, base)
                n += 1
    return n


def _json_for_script(bundle: dict) -> str:
    """<script> に安全に入れられる JSON にする。
    '<' を \\u003c にすると </script> や <!-- が現れず、JSON としては同じ値に戻る（<em> も JSON.parse で復元）"""
    return json.dumps(bundle, ensure_ascii=False).replace("<", "\\u003c")


def _read_part(name: str) -> str:
    """部品を 1 つ読む。無いときや、埋め込むと HTML が壊れる文字列を含むときは止める（黙って欠落させない）"""
    path = PARTS / name
    if not path.is_file():
        raise FileNotFoundError(f"player の部品が見つからない: {path}")
    text = path.read_text(encoding="utf-8")
    for bad in _FORBIDDEN.get(path.suffix, ()):
        if bad in text.lower():
            raise RuntimeError(f"{name} に {bad} は書けない（埋め込むと HTML が壊れる）")
    return text if text.endswith("\n") else text + "\n"


def assemble_template() -> str:
    """parts/ の部品を骨組みに差し込み、1 枚のテンプレート（bundle はまだ空）を返す"""
    # 関数を渡して、部品の JS 中のバックスラッシュを re.sub に解釈させない
    tpl = _INCLUDE.sub(lambda m: _read_part(m.group(1)), TEMPLATE.read_text(encoding="utf-8"))
    if "/*@include" in tpl:
        raise RuntimeError("差し込めなかった /*@include が残っている（行頭から書き、行末に何も置かない）")
    return tpl


def build(bundle_path, out_html, embed_assets: bool = True) -> Path:
    bundle_path = Path(bundle_path)
    out_html = Path(out_html)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    n = _embed(bundle, bundle_path.parent) if embed_assets else 0

    tpl = assemble_template()
    if len(_SLOT.findall(tpl)) != 1:
        raise RuntimeError("テンプレートに bundle 用の <script> が 1 つだけ必要")
    payload = _json_for_script(bundle)
    # 関数を渡して、payload 中のバックスラッシュを re.sub に解釈させない
    html = _SLOT.sub(lambda m: m.group(1) + payload + m.group(3), tpl, count=1)

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")
    print(f"wrote {out_html}  ({out_html.stat().st_size / 1024:.0f} KB, 埋め込み {n} 件, 図 {len(bundle.get('figures', []))} 枚)")
    return out_html


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: py -3.13 player/build_player.py <bundle.json> <out.html> [--no-embed]")
        print("       py -3.13 player/build_player.py --template-only <out.html>")
        sys.exit(2)
    if sys.argv[1] == "--template-only":
        Path(sys.argv[2]).write_text(assemble_template(), encoding="utf-8")
    else:
        build(sys.argv[1], sys.argv[2], embed_assets="--no-embed" not in sys.argv[3:])
