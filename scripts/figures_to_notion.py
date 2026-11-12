"""論文 PDF の図・表を切り出し、Notion のページの末尾に画像として貼る（LLM を使わない）。

講義（Claude×Notion で論文 DB）の発展編のための小さな入口。切り出しは pfe.figures（キャプションの位置と段組みから
図の範囲を決める）、送信は Notion の REST API。Claude に頼む場合も、Claude はこのコマンドを 1 回打って短い結果を読むだけで、
画像やアップロードの応答は文脈に入らない（トークンの節約）。

使い方（リポジトリの一番上のフォルダで。Windows は py -3.13、macOS・Linux は python3）:
  py -3.13 scripts/figures_to_notion.py "<論文.pdf>"
      切り出しだけ。work/_to_notion/<名前>/fig/<id>.png と、全部を並べた一覧画像 work/_to_notion/<名前>/figures_sheet.png を作る
  py -3.13 scripts/figures_to_notion.py "<論文.pdf>" --page "<Notion のページの URL>"
      切り出して、そのページの末尾に「図（PDF から切り出し）」の見出しと図を貼る
  py -3.13 scripts/figures_to_notion.py --save-token
      Notion のトークンを ~/.config/pfe/env に保存する（入力は画面に出ない。自分のターミナルで打つ）

オプション:
  --only fig1,table2   一部だけ貼る（id は切り出しだけの実行で出る）
  --force              同じ見出しがすでにページにあっても貼る（ふだんは二重に貼らないよう止まる）
  --attach-pdf         PDF 本体も添付する（「↓↓↓PDFファイル…」の見出しの直下。20 MiB まで。添付済みなら送らない）
  --pdf-only           PDF の添付だけ（図は切り出さない。--page が要る）

終了コード: 0 成功 / 1 失敗 / 2 図が 0 件・準備が足りない / 3 すでに貼ってある

守ること:
- トークンを表示しない・ログに書かない。送り先は api.notion.com だけ
- 届いたか分からない失敗（5xx・時間切れ）の追記を自動でやり直さない（二重に貼る）。ページを見てから --force で
"""
from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
ENV_FILE = Path.home() / ".config" / "pfe" / "env"
HEADING = "図（PDF から切り出し）"            # 二重に貼らないための目印にも使う
MAX_UPLOAD = 20 * 1024 * 1024                 # 単発アップロードの上限（20 MiB）
MAX_TEXT = 2000                               # rich_text 1 つの上限
MAX_CHILDREN = 100                            # 1 回の追記で送れるブロック数
HINTS = {
    401: "トークンが違う（--save-token で入れ直す）",
    403: "このコネクトに書き込みの権限が無い（機能の「更新」「挿入」にチェック）",
    404: "ページがこのコネクトに接続されていない（ページか DB の「…」→「詳細」→「接続」で作ったコネクトを追加）",
}
EXIT_OK, EXIT_FAILED, EXIT_PREFLIGHT, EXIT_DUPLICATE = 0, 1, 2, 3


# ---- 切り出し ----

def slug_of(pdf: Path) -> str:
    """作業フォルダ名。ファイル名から記号を除いて短くする（日本語はそのまま残す）。"""
    s = re.sub(r"[^\w\-]+", "_", pdf.stem).strip("_")
    return (s or "paper")[:60]


def crop(pdf: Path, out_dir: Path) -> list[dict]:
    """図・表を検出して PNG に書き出し、[{id, label, caption, page, confidence, png}] を返す。"""
    import pymupdf
    from pfe.figures import detect_figures, render_figure
    doc = pymupdf.open(str(pdf))
    figs = detect_figures(doc)
    for f in figs:
        f["png"] = out_dir / "fig" / f"{f['id']}.png"
        render_figure(doc, f, f["png"])
    doc.close()
    return figs


def contact_sheet(figs: list[dict], out_png: Path, cell: int = 420) -> None:
    """切り出した図を 1 枚に並べる（番号つき）。確かめるときはこの 1 枚だけを見ればよい。"""
    from PIL import Image, ImageDraw
    cols = 3 if len(figs) > 4 else max(1, len(figs))
    rows = -(-len(figs) // cols)
    label_h = 28
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, f in enumerate(figs):
        x, y = (i % cols) * cell, (i // cols) * (cell + label_h)
        im = Image.open(f["png"]).convert("RGB")
        im.thumbnail((cell - 12, cell - 12))
        sheet.paste(im, (x + (cell - im.width) // 2, y + label_h + (cell - im.height) // 2))
        mark = "  [要確認]" if f.get("confidence") == "low" else ""
        draw.text((x + 8, y + 6), f"{f['id']}  p.{f['page'] + 1}{mark}", fill="red" if mark else "black")
        draw.rectangle([x, y, x + cell - 1, y + cell + label_h - 1], outline="#cccccc")
    sheet.save(out_png)


# ---- トークン ----

def load_token() -> str:
    """環境変数 NOTION_TOKEN、無ければ ~/.config/pfe/env の NOTION_TOKEN= の行。値は表示しない。"""
    tok = os.environ.get("NOTION_TOKEN", "").strip()
    if tok:
        return tok
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == "NOTION_TOKEN":
                return v.strip().strip('"').strip("'")
    return ""


def save_token() -> int:
    """トークンを隠し入力で受け取り ~/.config/pfe/env に書く（ほかの行は残す）。"""
    import getpass
    tok = getpass.getpass("Notion のトークンを貼り付けて Enter（画面には出ません）: ").strip()
    if not re.fullmatch(r"(ntn_|secret_)[A-Za-z0-9]{20,}", tok):
        print("トークンの形ではない（ntn_ か secret_ で始まる長い文字列）。保存しなかった")
        return EXIT_PREFLIGHT
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    keep = []
    if ENV_FILE.is_file():
        keep = [ln for ln in ENV_FILE.read_text(encoding="utf-8").splitlines()
                if ln.partition("=")[0].strip() != "NOTION_TOKEN"]
    ENV_FILE.write_text("\n".join(keep + [f"NOTION_TOKEN={tok}"]) + "\n", encoding="utf-8")
    print(f"保存した: {ENV_FILE}（NOTION_TOKEN の行）")
    return EXIT_OK


# ---- Notion ----

def page_id_of(text: str) -> str:
    """URL か ID から 32 桁のページ ID を取り出す（クエリの ?v= などは見ない）。"""
    path = text.split("?")[0].split("#")[0]
    found = re.findall(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}", path)
    return found[-1].replace("-", "").lower() if found else ""


class Notion:
    """httpx の薄い包み。429 だけ待ってやり直す（処理されていないので安全）。"""

    def __init__(self, token: str):
        import httpx
        self.httpx = httpx
        self.http = httpx.Client(base_url=API, timeout=120,
                                 headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION})

    def call(self, method: str, path: str, **kw) -> dict:
        for _ in range(5):
            try:
                r = self.http.request(method, path, **kw)
            except self.httpx.TransportError as e:
                raise RuntimeError(f"{method} {path}: 応答が取れなかった（{type(e).__name__}）") from None
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("Retry-After", 2)), 30))
                continue
            if r.status_code == 200:
                return r.json()
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = ""
            hint = HINTS.get(r.status_code, "")
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {hint} {msg[:200]}".strip())
        raise RuntimeError(f"{method} {path}: 混んでいて通らなかった（429 が続いた）")

    def children(self, page: str) -> list[dict]:
        """ページの直下のブロックを全部（ページ送りしながら）。"""
        out, cursor = [], None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            j = self.call("GET", f"/blocks/{page}/children", params=params)
            out += j.get("results", [])
            if not j.get("has_more"):
                return out
            cursor = j.get("next_cursor")

    def already_has_heading(self, page: str) -> bool:
        """ページの直下に、前に貼った見出しがあるか。"""
        return any(b.get("type", "").startswith("heading") and plain(b) == HEADING for b in self.children(page))

    def upload(self, path: Path, content_type: str) -> str:
        """発行→送信。送り先は ID から作る（トークンを api.notion.com 以外へ送らない）。"""
        data = path.read_bytes()
        made = self.call("POST", "/file_uploads", json={"filename": path.name, "content_type": content_type})
        sent = self.call("POST", f"/file_uploads/{made['id']}/send", files={"file": (path.name, data, content_type)})
        if sent.get("status") != "uploaded":
            raise RuntimeError(f"{path.name}: 送信が完了しなかった（{sent.get('status')}）")
        return made["id"]


def plain(block: dict) -> str:
    """見出し・段落・引用などの文字だけを取り出す。"""
    body = block.get(block.get("type"), {})
    return "".join(t.get("plain_text", "") for t in body.get("rich_text", []) or []).strip()


def attach_pdf(n: Notion, page: str, pdf: Path) -> None:
    """PDF 本体をページに添付する。論文DBの型の見出し「↓↓↓PDFファイル…」があればその直下に置き、
    「（PDF は手動で添付）」の引用は消す。見出しが無ければ末尾に「PDF」の見出しと一緒に置く。"""
    if pdf.stat().st_size > MAX_UPLOAD:
        print(f"PDF が 20 MiB を超えるので送れない（{pdf.stat().st_size / 2**20:.1f} MiB）。Notion の画面で手で添付する")
        return
    blocks = n.children(page)
    head = next((i for i, b in enumerate(blocks) if b.get("type", "").startswith("heading") and "PDF" in plain(b)), None)
    if head is not None and head + 1 < len(blocks) and blocks[head + 1].get("type") == "file":
        print("PDF はもう添付してある（見出しの直下にファイルがある）。送らなかった")
        return
    file_block = {"object": "block", "type": "file",
                  "file": {"type": "file_upload", "file_upload": {"id": n.upload(pdf, "application/pdf")}, "name": pdf.name}}
    if head is None:
        n.call("PATCH", f"/blocks/{page}/children", json={"children": [
            {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich_text("PDF")}}, file_block]})
        print("PDF を添付した（ページの末尾）")
        return
    n.call("PATCH", f"/blocks/{page}/children", json={"children": [file_block], "after": blocks[head]["id"]})
    nxt = blocks[head + 1] if head + 1 < len(blocks) else None
    if nxt and nxt.get("type") == "quote" and "手動で添付" in plain(nxt):
        n.call("DELETE", f"/blocks/{nxt['id']}")
    print("PDF を添付した（PDF の見出しの直下）")


def rich_text(s: str) -> list[dict]:
    s = re.sub(r"\s+", " ", s or "").strip()
    return [{"type": "text", "text": {"content": s[i:i + MAX_TEXT]}} for i in range(0, len(s), MAX_TEXT)][:100]


def image_block(upload_id: str, caption: str) -> dict:
    image = {"type": "file_upload", "file_upload": {"id": upload_id}}
    if caption:
        image["caption"] = rich_text(caption)
    return {"object": "block", "type": "image", "image": image}


def connect() -> Notion | None:
    token = load_token()
    if not token:
        print("NOTION_TOKEN が無い。自分のターミナルで次を打って保存する（チャットに貼らない）:\n"
              "  py -3.13 scripts/figures_to_notion.py --save-token")
        return None
    return Notion(token)


def post_to_notion(n: Notion, figs: list[dict], page: str, force: bool) -> int:
    if not force and n.already_has_heading(page):
        print(f"このページにはもう「{HEADING}」がある。二重に貼るなら --force（先にページを見て確かめる）")
        return EXIT_DUPLICATE
    too_big = [f["id"] for f in figs if f["png"].stat().st_size > MAX_UPLOAD]
    if too_big:
        print(f"20 MiB を超えて送れない: {', '.join(too_big)}")
        return EXIT_PREFLIGHT
    blocks = [{"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich_text(HEADING)}}]
    for f in figs:
        blocks.append(image_block(n.upload(f["png"], "image/png"), f.get("caption", "")))
        print(f"  送った: {f['id']}")
    for i in range(0, len(blocks), MAX_CHILDREN):
        n.call("PATCH", f"/blocks/{page}/children", json={"children": blocks[i:i + MAX_CHILDREN]})
    print(f"貼った: {len(figs)} 個（ページの末尾）")
    return EXIT_OK


# ---- 入口 ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="論文 PDF の図・表を切り出して Notion のページに貼る")
    ap.add_argument("pdf", nargs="?", help="論文の PDF")
    ap.add_argument("--page", help="貼る先の Notion ページの URL か ID（無ければ切り出しだけ）")
    ap.add_argument("--only", help="貼る図の id（カンマ区切り。例 fig1,table2）")
    ap.add_argument("--force", action="store_true", help="同じ見出しがあっても貼る")
    ap.add_argument("--attach-pdf", action="store_true", help="PDF 本体もページに添付する（20 MiB まで）")
    ap.add_argument("--pdf-only", action="store_true", help="PDF の添付だけ（図は切り出さない）")
    ap.add_argument("--save-token", action="store_true", help="Notion のトークンを保存する")
    a = ap.parse_args(argv)

    if a.save_token:
        return save_token()
    if not a.pdf:
        ap.print_help()
        return EXIT_PREFLIGHT
    pdf = Path(a.pdf.strip('"'))
    if not pdf.is_file():
        print(f"PDF が見つからない: {pdf}")
        return EXIT_PREFLIGHT
    page = page_id_of(a.page) if a.page else ""
    if a.page and not page:
        print("ページの URL から ID が読めない（Notion でページを開き「リンクをコピー」した URL を渡す）")
        return EXIT_PREFLIGHT
    if a.pdf_only:
        if not page:
            print("--pdf-only には --page が要る")
            return EXIT_PREFLIGHT
        n = connect()
        if n is None:
            return EXIT_PREFLIGHT
        try:
            attach_pdf(n, page, pdf)
            return EXIT_OK
        except RuntimeError as e:
            print(f"失敗: {e}")
            return EXIT_FAILED

    # 棚（work/<pid>/）と同じ名前になると図を上書きするので、専用の置き場に分ける（2026-11-12 に kawamoto2017_jws で実際に起きた）
    out_dir = ROOT / "work" / "_to_notion" / slug_of(pdf)
    figs = crop(pdf, out_dir)
    if not figs:
        print("図・表が 0 件（キャプション「Fig. 1」「Figure 1」「Table 1」が見つからない形式）。手で切り出す必要がある")
        return EXIT_PREFLIGHT
    sheet = out_dir / "figures_sheet.png"
    contact_sheet(figs, sheet)
    low = [f["id"] for f in figs if f.get("confidence") == "low"]
    print(f"切り出した: {len(figs)} 個 → {out_dir / 'fig'}")
    for f in figs:
        cap = re.sub(r"\s+", " ", f.get("caption", ""))[:50]
        print(f"  {f['id']:8} p.{f['page'] + 1:<3} {'[要確認] ' if f.get('confidence') == 'low' else ''}{cap}")
    print(f"一覧画像: {sheet}")
    if low:
        print(f"枠が怪しい: {', '.join(low)}（一覧画像で確かめる。貼らないなら --only で外す）")

    if a.only:
        want = [s.strip() for s in a.only.split(",") if s.strip()]
        unknown = [w for w in want if w not in {f["id"] for f in figs}]
        if unknown:
            print(f"--only の id が無い: {', '.join(unknown)}")
            return EXIT_PREFLIGHT
        figs = [f for f in figs if f["id"] in want]
    if not page:
        return EXIT_OK
    n = connect()
    if n is None:
        return EXIT_PREFLIGHT
    try:
        if a.attach_pdf:
            attach_pdf(n, page, pdf)
        return post_to_notion(n, figs, page, a.force)
    except RuntimeError as e:
        print(f"失敗: {e}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
