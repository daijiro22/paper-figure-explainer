"""ブラウザから使う入口（FastAPI）。127.0.0.1 で待ち受け、外へは Cloudflare Tunnel でつなぐ。

見る（誰でも）:
  GET  /                    一覧の画面（web/static/index.html）
  GET  /api/papers          作ったものの一覧
  GET  /api/papers/{pid}    1 本の状態
  GET  /p/{pid}             解説（work/<pid>/player.html だけ。画像も音声も中に入っている）
作る（研究室のメールだけ。Cloudflare Access で /make を守り、ここでも Access の証明を確かめる）:
  GET  /make                入力欄つきの画面
  POST /make/api/upload     PDF の中身をそのまま本文で送る（見出し X-Filename・X-Figs・X-No-Tts）
  POST /make/api/papers/{pid}/retry
  POST /make/api/papers/{pid}/meta     {"title": 直した題（空で元に戻す）, "public": 公開サイトに載せるか}
  GET  /make/doc/{pid}                 原文の画面（ページの縮小一覧＋大きなページ。図を押すと解説へ）
  GET  /make/api/papers/{pid}/pages    ページ数・図の位置
  GET  /make/api/papers/{pid}/page/{n}?w=240|1400   ページの画像
見る側に足したもの: GET /api/papers/{pid}/thumb（カードの縮小画像。最初の図）
work/ の中の PDF 原本・ページ全体の画像・途中のファイルは公開しない（ページの画像は /make の内側だけ）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from pfe.meta import is_public, notion_url, paper_meta, read_override, thumbnail, write_override
from pfe.shelf import shelf_info
from pfe.pages import page_info, render_page
from pfe import tags as tagstore
from web.db import DB, default_path
from web.jobs import WORK, InputError, Jobs

STATIC = Path(__file__).resolve().parent / "static"
PID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}     # この PC から直接開くときの名前
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}           # 何も書き換えない要求


def foreign_request(headers, method: str) -> str | None:
    """別のサイトに仕向けられた要求なら理由を返す（どのページからでも 127.0.0.1 へ要求は送れるので）。

    - 直接の要求の Host が手元の名前でない: DNS を差し替えて 127.0.0.1 に届かせる手口（DNS rebinding）
    - 書き込みの要求の Origin が Host と違う: 別のサイトのページが送った要求（CSRF）。解析を動かすと API の枠を使う
    Origin の無い要求（curl など）は通す。ブラウザは POST に Origin を付ける。トンネル経由は Host が公開の名前になる。
    """
    host = headers.get("host", "")
    try:
        if not headers.get("cf-connecting-ip") and urlsplit(f"//{host}").hostname not in LOCAL_HOSTS:
            return f"手元の名前でない Host を断った: {host}"
        origin = headers.get("origin")
        if method not in SAFE_METHODS and origin is not None and urlsplit(origin).netloc != host:
            return f"別のサイトからの書き込みを断った: {origin}"
    except ValueError:      # 形の崩れた Host・Origin
        return "読めない Host か Origin を断った"
    return None


class TagEdit(BaseModel):
    """タグを作る（name）・名前を変える（name）・消す（deleted=true）。"""
    name: str | None = None
    deleted: bool | None = None


class PaperTags(BaseModel):
    """論文に付けるタグの id の並び（全部を送る。付いていないものは外れる）。"""
    tags: list[str]


class MetaEdit(BaseModel):
    """POST /make/api/papers/{pid}/meta の本文。送らなかった項目は変えない。
    （関数の中で定義すると注釈が解決できず、FastAPI が本文として読まない）"""
    title: str | None = None
    public: bool | None = None


def _retitle_player(d: Path, meta: dict) -> None:
    """bundle.json の題を直し、player.html を組み直す（解析・声はそのまま。数秒で終わる）。"""
    bpath = d / "bundle.json"
    if not bpath.exists():
        return
    b = json.loads(bpath.read_text(encoding="utf-8"))
    if (b.get("paper") or {}).get("title") == meta.get("title"):
        return
    b["paper"] = {**(b.get("paper") or {}), "title": meta.get("title")}
    bpath.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "player"))
    from build_player import build
    build(str(bpath), str(d / "player.html"))


def parse_figs(s: str | None) -> list[str] | None:
    """"1, 3, fig5" → ["fig1", "fig3", "fig5"]。空なら None（全部）。"""
    if not s or not s.strip():
        return None
    out = []
    for part in re.split(r"[,\s、]+", s.strip()):
        m = re.fullmatch(r"(?:fig\.?\s*)?(\d+)", part, re.I)
        if not m:
            raise InputError(f"図の指定が読めない: {part}（例: 1,3 か fig1,fig3）")
        out.append(f"fig{int(m.group(1))}")
    return out


class AccessGuard:
    """/make への要求が「研究室のメールで Access を通った人」からかを確かめる。

    - Cloudflare を通った要求（見出し cf-connecting-ip がある）は、Access の JWT（cf-access-jwt-assertion）を
      チーム（PFE_ACCESS_TEAM 例 xxx.cloudflareaccess.com）の公開鍵と AUD（PFE_ACCESS_AUD）で検証する。
      設定が無ければ拒む（設定漏れで誰でも作れる状態にしない）。
    - この PC から直接（127.0.0.1）の要求は見出しが無いので通す（手元で使うとき）。トンネルの外からは届かない。
    """

    def __init__(self, team: str | None = None, aud: str | None = None):
        self.team = (team or os.environ.get("PFE_ACCESS_TEAM") or "").strip().rstrip("/")
        self.aud = (aud or os.environ.get("PFE_ACCESS_AUD") or "").strip()
        self._jwks = None

    def check(self, request: Request) -> str:
        """通れば利用者のメール（直接の要求は "local"）を返す。だめなら HTTPException。"""
        if not request.headers.get("cf-connecting-ip"):
            return "local"
        if not (self.team and self.aud):
            raise HTTPException(403, "作る操作は無効です（サーバに Access の設定が無い）")
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        if not token:
            raise HTTPException(403, "作るには研究室のメールでログインしてください（/make を開き直す）")
        import jwt
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(f"https://{self.team}/cdn-cgi/access/certs")
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.aud,
                                issuer=f"https://{self.team}")
        except jwt.PyJWTError as e:
            raise HTTPException(403, f"ログインの証明を確かめられない: {e}")
        return claims.get("email") or claims.get("sub") or "unknown"


def create_app(db: DB | None = None, jobs: Jobs | None = None, start_worker: bool = True,
               guard: AccessGuard | None = None) -> FastAPI:
    db = db or DB(default_path())
    jobs = jobs or Jobs(db, driver=os.environ.get("PFE_DRIVER", "api"))
    guard = guard or AccessGuard()
    app = FastAPI(title="論文の図の解説", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.jobs = db, jobs
    if start_worker:
        jobs.start()
    else:
        WORK.mkdir(exist_ok=True)

    @app.middleware("http")
    async def _only_this_site(request: Request, call_next):
        problem = foreign_request(request.headers, request.method)
        if problem:
            return JSONResponse({"error": problem}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)       # InputError と、タグ・題の保存の入力誤り（pfe/tags.py・meta.py）
    async def _bad_input(request: Request, e: ValueError):
        return JSONResponse({"error": str(e)}, status_code=400)

    def view(row: dict) -> dict:
        out = jobs.view(row)
        out.pop("sha256", None)
        out.pop("requested_by", None)      # 頼んだ人のメールは公開しない
        out["player"] = f"/p/{row['pid']}" if out["player"] else None
        d = WORK / row["pid"]
        out["public"] = is_public(d)
        out["title_extracted"] = read_override(d).get("title") and paper_meta(d).get("title_extracted")
        out["thumb"] = f"/api/papers/{row['pid']}/thumb" if out["player"] else None
        out["notion"] = (d / "notion" / "page.json").is_file()     # 「あるか」だけ。URL は /make/notion/ の内側で解決する
        out["shelf"] = shelf_info(paper_meta(d))                   # カードの年・雑誌の略称・著者と、検索に足す文字列
        return out

    def html_page(path: Path, request: Request) -> Response:
        """HTML のページ。Cache-Control が無いと、ブラウザが古い写しをサーバに聞かず使い回す（直した棚や解説が出ない）。
        no-cache は毎回「変わったか」を聞くだけ。FileResponse は If-None-Match を見ないので、ここで 304 を返す"""
        no_cache = {"Cache-Control": "no-cache"}
        r = FileResponse(path, media_type="text/html; charset=utf-8", headers=no_cache, stat_result=path.stat())
        if request.headers.get("if-none-match") == r.headers["etag"]:
            return Response(status_code=304, headers={**no_cache, "ETag": r.headers["etag"]})
        return r

    # ---- 見る ----
    @app.get("/")
    def index(request: Request):
        return html_page(STATIC / "index.html", request)

    @app.get("/api/papers")
    def papers():
        return [view(r) for r in db.list()]

    @app.get("/api/papers/{pid}")
    def paper(pid: str):
        row = db.get(pid)
        if not row:
            raise HTTPException(404, "その論文は一覧に無い")
        return view(row)

    @app.get("/api/papers/{pid}/thumb")
    def thumb(pid: str):
        t = thumbnail(WORK / pid) if PID_RE.match(pid) and (WORK / pid).is_dir() else None
        if t is None:
            raise HTTPException(404, "サムネイルが無い")
        return FileResponse(t, media_type="image/jpeg")

    @app.get("/p/{pid}")
    def player(pid: str, request: Request):
        p = WORK / pid / "player.html"
        if not PID_RE.match(pid) or not p.is_file():
            raise HTTPException(404, "解説が無い")
        return html_page(p, request)

    # ---- 作る（/make 以下だけ） ----
    @app.get("/make")
    def make_page(request: Request):
        guard.check(request)
        return html_page(STATIC / "index.html", request)

    @app.post("/make/api/upload")
    async def upload(request: Request):
        who = guard.check(request)
        name = unquote(request.headers.get("x-filename", "paper.pdf"))
        figs = parse_figs(unquote(request.headers.get("x-figs", "")))
        no_tts = request.headers.get("x-no-tts") == "1"
        data = await request.body()
        pid = jobs.submit_pdf(data, name, figs, no_tts, by=who)
        # 開いていたタグ（X-Tags）を付ける。同じ PDF をもう一度置いたときは、前のタグに足す（置き換えない）
        add = [t for t in unquote(request.headers.get("x-tags", "")).split(",") if t]
        if add:
            live = _live()
            now = [t for t in tagstore.paper_tags(WORK / pid) if t in live]     # 一覧に無い id は set_paper_tags が残す
            tagstore.set_paper_tags(WORK / pid, now + [t for t in add if t in live], set(live))
        return {"pid": pid}

    # ---- 原文の画面（手元だけ。ページ全体の画像は有料誌の本文なので、Access の守りの内側に置く） ----
    def _doc_dir(pid: str, request: Request) -> Path:
        guard.check(request)
        d = WORK / pid
        if not PID_RE.match(pid) or not (d / "source.pdf").is_file():
            raise HTTPException(404, "その論文の PDF が無い")
        return d

    @app.get("/make/doc/{pid}")
    def doc_page(pid: str, request: Request):
        _doc_dir(pid, request)
        return html_page(STATIC / "doc.html", request)

    @app.get("/make/api/papers/{pid}/pages")
    def pages(pid: str, request: Request):
        d = _doc_dir(pid, request)
        row = db.get(pid)
        return {"pid": pid, "title": (row or {}).get("title") or paper_meta(d).get("title") or pid,
                "player": f"/p/{pid}" if (d / "player.html").exists() else None,
                "notion": notion_url(d) is not None, **page_info(d)}

    # 施主 2026-11-06「解説サイトに Notion へのリンクも」。研究室の DB なので原文と同じく守りの内側に置く
    @app.get("/make/notion/{pid}")
    def notion_page(pid: str, request: Request):
        guard.check(request)
        url = notion_url(WORK / pid) if PID_RE.match(pid) else None
        if not url:
            raise HTTPException(404, "その論文は Notion に控えが無い")
        return RedirectResponse(url, status_code=302)

    @app.get("/make/api/papers/{pid}/page/{n}")
    def page_png(pid: str, n: int, request: Request, w: int = 1400):
        d = _doc_dir(pid, request)
        try:
            return FileResponse(render_page(d, n, w), media_type="image/png")
        except (ValueError, IndexError):
            raise HTTPException(404, "そのページは無い")

    # ---- タグ（自分用。見る側の /api/papers には出さない） ----
    def _live() -> dict[str, dict]:
        return {t["id"]: t for t in tagstore.active(WORK)}

    @app.get("/make/api/papers")
    def make_papers(request: Request):
        guard.check(request)
        live = _live()
        return [{**view(r), "tags": tagstore.visible(WORK / r["pid"], live)} for r in db.list()]

    @app.get("/make/api/tags")
    def tags_list(request: Request):
        guard.check(request)
        live = _live()
        count = {i: 0 for i in live}
        for r in db.list():
            for t in tagstore.visible(WORK / r["pid"], live):
                count[t["id"]] += 1
        return [{"id": t["id"], "name": t["name"], "count": count[t["id"]]} for t in live.values()]

    @app.post("/make/api/tags")
    def tags_create(t: TagEdit, request: Request):
        guard.check(request)
        return tagstore.create(WORK, t.name or "")

    @app.post("/make/api/tags/{tid}")
    def tags_update(tid: str, t: TagEdit, request: Request):
        guard.check(request)
        try:
            return tagstore.update(WORK, tid, name=t.name, deleted=t.deleted)
        except KeyError:
            raise HTTPException(404, "そのタグは無い")

    @app.post("/make/api/papers/{pid}/tags")
    def paper_tags_set(pid: str, body: PaperTags, request: Request):
        guard.check(request)
        d = WORK / pid
        if not PID_RE.match(pid) or not d.is_dir():
            raise HTTPException(404, "その論文は無い")
        live = _live()
        tagstore.set_paper_tags(d, body.tags, set(live))
        return {"pid": pid, "tags": tagstore.visible(d, live)}

    @app.post("/make/api/papers/{pid}/meta")
    def edit_meta(pid: str, m: MetaEdit, request: Request):
        """題を直す（空文字なら取り込み時の題に戻す）・公開するかを切り替える。解析や声は作り直さない。"""
        guard.check(request)
        d = WORK / pid
        if not PID_RE.match(pid) or not d.is_dir():
            raise HTTPException(404, "その論文は無い")
        fields = {}
        if m.title is not None:
            t = " ".join(m.title.split())
            if len(t) > 300:
                raise InputError("題が長すぎます（300 字まで）")
            fields["title"] = t or None
        if m.public is not None:
            fields["public"] = None if m.public else False      # 既定（載せる）は書かない
        write_override(d, **fields)
        meta = paper_meta(d)
        db.upsert(pid, title=meta.get("title"))
        _retitle_player(d, meta)
        row = db.get(pid)
        return view(row) if row else {"pid": pid}

    @app.post("/make/api/papers/{pid}/retry")
    def retry(pid: str, request: Request):
        guard.check(request)
        jobs.retry(pid)
        return {"pid": pid}

    return app
