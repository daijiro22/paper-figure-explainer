"""Web 版（web/）のテスト。パイプライン本体は動かさず、run.py の代わりの偽物で順番と状態の移り方を確かめる。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import web.app as A  # noqa: E402
import web.jobs as J  # noqa: E402
from web.db import DB  # noqa: E402

PDF = b"%PDF-1.4\n% fake\n"


@pytest.fixture(autouse=True)
def _testclient_is_local(monkeypatch):
    """TestClient の Host は testserver。手元の名前として扱う。"""
    monkeypatch.setattr(A, "LOCAL_HOSTS", A.LOCAL_HOSTS | {"testserver"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(J, "WORK", work)
    monkeypatch.setattr(A, "WORK", work)
    db = DB(tmp_path / "pfe.db")
    return work, db


class FakeJobs(J.Jobs):
    """run.py の代わり: --figs の図を bundle.json に書く。fail に入れた図は書かない（解析できず）。"""

    def __init__(self, db, fail=()):
        super().__init__(db)
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    def _run(self, args, log):
        if "--figs" in args:
            figs = args[args.index("--figs") + 1].split(",")
            self.calls.append(figs)
            d = Path(args[args.index("--pdf") + 1]).parent
            (d / "bundle.json").write_text(json.dumps({"figures": [{"id": f} for f in figs if f not in self.fail]}))
            (d / "player.html").write_text("<html></html>")
        return 0, ""


def _paper(work: Path, pid: str, n: int) -> Path:
    d = work / pid
    d.mkdir()
    (d / "source.pdf").write_bytes(PDF + pid.encode())
    (d / "figures.json").write_text(json.dumps([{"id": f"fig{i}"} for i in range(1, n + 1)] + [{"id": "table1"}]))
    (d / "meta.json").write_text(json.dumps({"title": "T", "authors": ["A", "B"], "year": 2026, "doi": "10.1/x"}))
    return d


def test_parse_figs():
    assert A.parse_figs("1, 3、Fig.5 fig7") == ["fig1", "fig3", "fig5", "fig7"]
    assert A.parse_figs("  ") is None
    with pytest.raises(J.InputError):
        A.parse_figs("1,a")


def test_paper_id_matches_run_py():
    assert J.paper_id_from("Taira et al. - 2026 - Pyrolysis In situ assessment.pdf") == "taira2026_assessment"
    assert J.paper_id_from("Huang et al. - 2024 - Pyrolysis of lignin.pdf") == "huang2024_paper"  # 4 字以上の語が 3 つ
    assert J.paper_id_from("???.pdf") == "paper"


def test_figures_one_by_one_and_failed_figure_dropped(env):
    """図を 1 枚ずつ、できた図＋今回の図で作り直す。解析できなかった図は次から --figs に入れない。"""
    work, db = env
    _paper(work, "p1", 4)
    db.upsert("p1", sha256="x", source="p1.pdf", status="queued", figs=[])
    jobs = FakeJobs(db, fail={"fig3"})
    jobs._process("p1")
    assert jobs.calls == [["fig1"], ["fig1", "fig2"], ["fig1", "fig2", "fig3"], ["fig1", "fig2", "fig4"]]
    row = db.get("p1")
    assert row["status"] == "done" and row["done_figs"] == ["fig1", "fig2", "fig4"]
    assert row["failed_figs"] == ["fig3"] and "fig3" in row["message"]
    assert row["title"] == "T" and row["authors"] == "A, B"


def test_existing_figures_stay_in_player(env):
    """作成済みの論文に図を足しても、前の図を player から消さない（run.py は --figs の図だけで作り直す）。"""
    work, db = env
    d = _paper(work, "p2", 5)
    (d / "bundle.json").write_text(json.dumps({"figures": [{"id": "fig1"}, {"id": "fig4"}]}))
    db.upsert("p2", sha256="y", source="p2.pdf", status="queued", figs=["fig2"])
    jobs = FakeJobs(db)
    jobs._process("p2")
    assert jobs.calls == [["fig1", "fig2", "fig4"]]
    assert db.get("p2")["done_figs"] == ["fig1", "fig2", "fig4"]
    assert db.get("p2")["figs"] == ["fig1", "fig2", "fig4"]     # 「3/1 図」にしない


def test_other_sites_cannot_use_the_local_server(env):
    """別のサイトのページからの書き込みと、DNS を差し替えて届く要求を断る。同じサイトの画面と curl は通す。"""
    work, db = env
    base = "http://127.0.0.1:8128"
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False), base_url=base)
    assert c.post("/make/api/tags", json={"name": "same"}, headers={"Origin": base}).status_code == 200
    assert c.post("/make/api/tags", json={"name": "curl"}).status_code == 200
    for origin in ("https://evil.example", "http://127.0.0.1:3000", "null"):
        r = c.post("/make/api/tags", json={"name": "x"}, headers={"Origin": origin})
        assert r.status_code == 403 and "別のサイト" in r.json()["error"]
    assert c.get("/api/papers").status_code == 200
    assert c.get("/api/papers", headers={"Host": "localhost:8128"}).status_code == 200
    assert c.get("/api/papers", headers={"Host": "[::1]:8128"}).status_code == 200
    assert c.get("/api/papers", headers={"Host": "evil.example:8128"}).status_code == 403
    assert c.get("/api/papers", headers={"Host": "[bad"}).status_code == 403
    # トンネル経由は Host が公開の名前。見るのは通し、別のサイトからの書き込みはここで断る
    tunnel = {"Host": "papers.example.com", "cf-connecting-ip": "203.0.113.1"}
    assert c.get("/api/papers", headers=tunnel).status_code == 200
    r = c.post("/make/api/tags", json={"name": "x"}, headers={**tunnel, "Origin": "https://evil.example"})
    assert r.status_code == 403 and "別のサイト" in r.json()["error"]


def test_pdf_only_no_doi(env):
    """DOI・URL の受付は外した（施主 2026-10-30）。PDF が消えた論文は「やり直す」を断る。"""
    work, db = env
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    assert c.post("/make/api/source", json={"text": "10.1234/x"}).status_code in (404, 405)
    db.upsert("gone", source="gone.pdf", status="failed", figs=[])
    r = c.post("/make/api/papers/gone/retry")
    assert r.status_code == 400 and "PDF" in r.json()["error"]


def test_scan_work_lists_existing(env):
    work, db = env
    d = _paper(work, "old", 2)
    (d / "bundle.json").write_text(json.dumps({"figures": [{"id": "fig1"}, {"id": "fig2"}]}))
    (d / "player.html").write_text("x")
    _paper(work, "broken", 1)
    assert db.scan_work(work) == 2
    assert db.get("old")["status"] == "done" and db.get("old")["done_figs"] == ["fig1", "fig2"]
    assert db.get("broken")["status"] == "failed"
    assert db.scan_work(work) == 0              # 2 回目は足さない
    # 作りかけで取り込まれた本も、解説ができたら次の取り込みで棚に出る
    (work / "broken" / "bundle.json").write_text(json.dumps({"figures": [{"id": "fig1"}]}))
    (work / "broken" / "player.html").write_text("x")
    db.scan_work(work)
    assert db.get("broken")["status"] == "done" and db.get("broken")["message"] is None
    # 図の失敗で止まった本（解説は一部できている）は、取り込み直しても failed のまま
    db.upsert("old", status="failed", message="fig2 で失敗")
    db.scan_work(work)
    assert db.get("old")["status"] == "failed"


def test_api_upload_and_dedupe(env):
    work, db = env
    jobs = FakeJobs(db)
    c = TestClient(A.create_app(db, jobs, start_worker=False))
    assert c.post("/make/api/upload", content=b"hello", headers={"X-Filename": "a.pdf"}).status_code == 400
    r = c.post("/make/api/upload", content=PDF, headers={"X-Filename": "Taira%20et%20al.%20-%202026%20-%20Pyrolysis%20In%20situ%20assessment.pdf",
                                                  "X-Figs": "1,2"})
    pid = r.json()["pid"]
    assert pid == "taira2026_assessment" and (work / pid / "source.pdf").read_bytes() == PDF
    assert db.get(pid)["figs"] == ["fig1", "fig2"]
    # 同じ PDF をもう一度: 同じ行を返す（作り直さない）
    assert c.post("/make/api/upload", content=PDF, headers={"X-Filename": "other.pdf"}).json()["pid"] == pid
    # 同じ名前の別の PDF: hash の頭を付けて分ける
    r2 = c.post("/make/api/upload", content=PDF + b"2", headers={"X-Filename": "Taira et al. - 2026 - Pyrolysis In situ assessment.pdf"})
    assert r2.json()["pid"].startswith(pid + "_")
    assert len(c.get("/api/papers").json()) == 2


def test_public_serves_only_player(env):
    """見る側は player.html だけ。PDF 原本・途中のファイル・頼んだ人のメールは出さない。"""
    work, db = env
    d = _paper(work, "pub", 1)
    (d / "player.html").write_text("<html>ok</html>", encoding="utf-8")
    db.upsert("pub", sha256="s", source="x.pdf", status="done", figs=["fig1"], done_figs=["fig1"],
              requested_by="someone@example.com")
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    assert c.get("/p/pub").text == "<html>ok</html>"
    assert c.get("/p/nothing").status_code == 404
    assert c.get("/work/pub/source.pdf").status_code == 404
    assert c.get("/p/..%2Fpub").status_code == 404
    row = c.get("/api/papers").json()[0]
    assert row["player"] == "/p/pub" and "requested_by" not in row and "sha256" not in row


def test_rename_survives_rebuild_and_can_reset(env, tmp_path):
    """題を直すと、DB を作り直しても・player を組み直しても残る。空で送ると PDF から取った題に戻る。"""
    work, db = env
    d = _paper(work, "ren", 1)
    (d / "bundle.json").write_text(json.dumps({"paper": {"title": "T"}, "figures": [{"id": "fig1"}]}), encoding="utf-8")
    (d / "player.html").write_text("<html></html>", encoding="utf-8")
    db.scan_work(work)
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    r = c.post("/make/api/papers/ren/meta", json={"title": "  The   Nature of Hololignin "})
    assert r.status_code == 200 and r.json()["title"] == "The Nature of Hololignin"
    assert r.json()["title_extracted"] == "T"
    assert json.loads((d / "bundle.json").read_text(encoding="utf-8"))["paper"]["title"] == "The Nature of Hololignin"
    assert "The Nature of Hololignin" in (d / "player.html").read_text(encoding="utf-8")   # player も組み直した
    db2 = DB(tmp_path / "again.db")                       # DB を消して work/ から作り直しても残る
    db2.scan_work(work)
    assert db2.get("ren")["title"] == "The Nature of Hololignin"
    assert c.post("/make/api/papers/ren/meta", json={"title": ""}).json()["title"] == "T"
    assert c.post("/make/api/papers/nothing/meta", json={"title": "x"}).status_code == 404


def test_public_toggle_and_thumb(env):
    from PIL import Image
    from pfe.meta import is_public
    work, db = env
    d = _paper(work, "pub2", 1)
    (d / "fig").mkdir()
    Image.new("RGB", (900, 600), "white").save(d / "fig" / "fig1.png")
    (d / "bundle.json").write_text(json.dumps({"paper": {"title": "T"}, "figures": [{"id": "fig1", "img": "fig/fig1.png"}]}))
    (d / "player.html").write_text("<html></html>", encoding="utf-8")
    db.scan_work(work)
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    row = c.get("/api/papers/pub2").json()
    assert row["public"] is True and row["thumb"] == "/api/papers/pub2/thumb"
    t = c.get(row["thumb"])
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"
    assert max(Image.open(d / "thumb.jpg").size) == 480
    assert c.post("/make/api/papers/pub2/meta", json={"public": False}).json()["public"] is False
    assert not is_public(d)
    c.post("/make/api/papers/pub2/meta", json={"public": True})
    assert is_public(d) and "public" not in json.loads((d / "meta_override.json").read_text(encoding="utf-8"))
    assert c.get("/api/papers/nothing/thumb").status_code == 404


def test_tags_create_assign_rename_delete(env, tmp_path):
    """タグを作る・付ける・名前を変える（全部の論文に反映）・消す（復活しない）。DB を作り直しても残る。見る側には出さない。"""
    work, db = env
    for pid in ("a1", "a2"):
        d = _paper(work, pid, 1)
        (d / "bundle.json").write_text(json.dumps({"paper": {"title": pid}, "figures": [{"id": "fig1"}]}))
        (d / "player.html").write_text("<html></html>", encoding="utf-8")
    db.scan_work(work)
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    t1 = c.post("/make/api/tags", json={"name": " 発表準備 "}).json()
    assert t1["name"] == "発表準備"
    assert c.post("/make/api/tags", json={"name": "発表準備"}).json()["id"] == t1["id"]     # 同じ名前は同じタグ
    t2 = c.post("/make/api/tags", json={"name": "リグニン熱分解"}).json()
    assert c.post("/make/api/tags", json={"name": "  "}).status_code == 400
    c.post("/make/api/papers/a1/tags", json={"tags": [t1["id"], t2["id"]]})
    c.post("/make/api/papers/a2/tags", json={"tags": [t2["id"]]})
    assert c.post("/make/api/papers/a2/tags", json={"tags": ["t_nothing"]}).status_code == 400
    counts = {t["name"]: t["count"] for t in c.get("/make/api/tags").json()}
    assert counts == {"発表準備": 1, "リグニン熱分解": 2}
    c.post(f"/make/api/tags/{t1['id']}", json={"name": "発表用"})                         # 名前を変える
    rows = {r["pid"]: [t["name"] for t in r["tags"]] for r in c.get("/make/api/papers").json()}
    assert rows == {"a1": ["発表用", "リグニン熱分解"], "a2": ["リグニン熱分解"]}
    assert "tags" not in c.get("/api/papers").json()[0]                                   # 見る側には出さない
    db2 = DB(tmp_path / "again.db")                                                        # DB を作り直しても残る
    db2.scan_work(work)
    c2 = TestClient(A.create_app(db2, FakeJobs(db2), start_worker=False))
    assert {r["pid"]: len(r["tags"]) for r in c2.get("/make/api/papers").json()} == {"a1": 2, "a2": 1}
    c.post(f"/make/api/tags/{t2['id']}", json={"deleted": True})                          # 消す
    assert [t["name"] for t in c.get("/make/api/tags").json()] == ["発表用"]
    assert {r["pid"]: [t["name"] for t in r["tags"]] for r in c.get("/make/api/papers").json()} == {"a1": ["発表用"], "a2": []}
    assert t2["id"] in json.loads((work / "a2" / "meta_override.json").read_text(encoding="utf-8"))["tags"]
    t3 = c.post("/make/api/tags", json={"name": "リグニン熱分解"}).json()                  # 同じ名前で作り直すと別のタグ
    assert t3["id"] != t2["id"]
    assert {r["pid"]: len(r["tags"]) for r in c.get("/make/api/papers").json()}["a2"] == 0   # 消したタグは復活しない


def test_upload_adds_open_tag(env):
    work, db = env
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False))
    t = c.post("/make/api/tags", json={"name": "読む"}).json()
    pid = c.post("/make/api/upload", content=PDF, headers={"X-Filename": "x.pdf", "X-Tags": t["id"]}).json()["pid"]
    assert [x["name"] for x in c.get("/make/api/papers").json()[0]["tags"]] == ["読む"]
    t2 = c.post("/make/api/tags", json={"name": "発表"}).json()                            # 同じ PDF をもう一度: タグは足す
    assert c.post("/make/api/upload", content=PDF, headers={"X-Filename": "x.pdf", "X-Tags": t2["id"]}).json()["pid"] == pid
    assert sorted(x["name"] for x in c.get("/make/api/papers").json()[0]["tags"]) == sorted(["読む", "発表"])


def test_override_unreadable_is_not_overwritten(env):
    """meta_override.json が壊れている（同期の途中など）ときは、空で上書きしない。"""
    from pfe.meta import write_override
    work, db = env
    d = _paper(work, "bad", 1)
    (d / "meta_override.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        write_override(d, title="x")
    assert (d / "meta_override.json").read_text(encoding="utf-8") == "{broken"


def test_doc_pages_local_only(env):
    """原文の画面: ページ数・図の位置（ページに対する割合）・ページの画像。トンネル経由でログインが無ければ断る。"""
    pymupdf = pytest.importorskip("pymupdf")
    work, db = env
    d = work / "docp"
    d.mkdir()
    pdf = pymupdf.open()
    for _ in range(2):
        pdf.new_page(width=600, height=800)
    pdf.save(d / "source.pdf")
    (d / "figures.json").write_text(json.dumps([{"id": "fig1", "label": "Fig. 1", "page": 2, "clip": [60, 80, 300, 400]}]))
    (d / "bundle.json").write_text(json.dumps({"paper": {"title": "T"}, "figures": [{"id": "fig1"}]}))
    (d / "player.html").write_text("<html></html>", encoding="utf-8")
    c = TestClient(A.create_app(db, FakeJobs(db), start_worker=False, guard=A.AccessGuard("", "")))
    j = c.get("/make/api/papers/docp/pages").json()
    assert j["n_pages"] == 2 and j["player"] == "/p/docp"
    assert j["figures"] == [{"id": "fig1", "label": "Fig. 1", "page": 2, "kind": "figure",
                             "box": [0.1, 0.1, 0.5, 0.5], "has_player": True}]
    r = c.get("/make/api/papers/docp/page/2?w=240")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert c.get("/make/api/papers/docp/page/3?w=240").status_code == 404
    assert c.get("/make/api/papers/docp/page/1?w=999").status_code == 404
    assert c.get("/make/doc/docp").status_code == 200
    # HTML のページは毎回サーバに確かめさせる（無いとブラウザが古い棚や解説を使い回す）
    for url in ("/", "/make", "/p/docp", "/make/doc/docp"):
        r = c.get(url)
        assert r.headers["cache-control"] == "no-cache", url
        # 変わっていなければ本体を送らない（解説は 1 本 16MB ほどあり、毎回送ると遅い）
        again = c.get(url, headers={"if-none-match": r.headers["etag"]})
        assert again.status_code == 304 and again.content == b"" and again.headers["cache-control"] == "no-cache", url
    cf = {"cf-connecting-ip": "203.0.113.5"}                 # トンネル経由・Access の設定なし
    assert c.get("/make/api/papers/docp/pages", headers=cf).status_code == 403
    assert c.get("/make/api/papers/docp/page/2?w=240", headers=cf).status_code == 403


def _rsa_and_token(aud, team, email="lab@example.ac.jp"):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tok = jwt.encode({"aud": [aud], "iss": f"https://{team}", "email": email, "exp": 4102444800},
                     key, algorithm="RS256")
    return key, tok


def test_make_requires_access_via_cloudflare(env):
    """Cloudflare を通った /make は Access の JWT が要る。手元から直接（見出しなし）は通す。"""
    pytest.importorskip("jwt")
    work, db = env
    team, aud = "lab.cloudflareaccess.com", "aud123"
    key, tok = _rsa_and_token(aud, team)

    class Jwks:
        def get_signing_key_from_jwt(self, t):
            return type("K", (), {"key": key.public_key()})()
    guard = A.AccessGuard(team, aud)
    guard._jwks = Jwks()
    jobs = FakeJobs(db)
    c = TestClient(A.create_app(db, jobs, start_worker=False, guard=guard))
    cf = {"cf-connecting-ip": "203.0.113.5"}
    assert c.get("/make").status_code == 200                                   # 手元から
    assert c.get("/make", headers=cf).status_code == 403                       # トンネル経由・未ログイン
    up = {"X-Filename": "Taira et al. - 2026 - Pyrolysis In situ assessment.pdf"}
    assert c.post("/make/api/upload", content=PDF, headers={**cf, **up}).status_code == 403
    _, other = _rsa_and_token(aud, team)                                       # 別の鍵で署名した偽物
    assert c.get("/make", headers={**cf, "cf-access-jwt-assertion": other}).status_code == 403
    r = c.post("/make/api/upload", content=PDF, headers={**cf, **up, "cf-access-jwt-assertion": tok})
    assert r.status_code == 200 and db.get(r.json()["pid"])["requested_by"] == "lab@example.ac.jp"
    # Access の設定が無いサーバは、トンネル経由の「作る」を全部断る
    c2 = TestClient(A.create_app(db, jobs, start_worker=False, guard=A.AccessGuard("", "")))
    assert c2.get("/make", headers={**cf, "cf-access-jwt-assertion": tok}).status_code == 403
