"""作ったものの一覧（SQLite）。

DB は work/ の索引にすぎない: 起動のたびに work/*/ を読み直して行を足すので、消えても壊れても作り直せる。
work/ を複数の PC で同期して使っても、DB は各機のローカルに置く（同期するフォルダの上の SQLite はロックが当てにならず、
-journal も同期されてしまう）。既定の場所は ~/.local/share/pfe/pfe.db（環境変数 PFE_DB で変えられる）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from pfe.meta import paper_meta

NO_PLAYER = "player.html が無い（途中で止まった）"     # scan_work が付ける印。解説ができたら次の scan_work で done に戻す
COLUMNS = ["pid", "sha256", "title", "authors", "year", "doi", "journal", "source",
           "status", "message", "figs", "done_figs", "failed_figs", "current", "requested_by", "created", "updated"]
# status: queued（待ち）| extracting（図を探し中）| running（解析中）
#         | done | failed（DOI からの取得はやめたので fetching・paywalled はもう作らない）

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  pid TEXT PRIMARY KEY,       -- work/<pid>/ のフォルダ名
  sha256 TEXT,                -- source.pdf の中身の hash（同じ PDF を二度作らない）
  title TEXT, authors TEXT, year INTEGER, doi TEXT, journal TEXT,
  source TEXT,                -- アップしたファイル名か DOI/URL
  status TEXT NOT NULL,
  message TEXT,               -- 失敗・警告の説明
  figs TEXT,                  -- 対象の図 id（JSON 配列）
  done_figs TEXT,             -- できた図 id（JSON 配列）
  failed_figs TEXT,           -- 解析できなかった図 id（JSON 配列）
  current TEXT,               -- いま処理中の図 id
  requested_by TEXT,          -- 頼んだ人（Access のメール。直接なら local）。一覧の API には出さない
  created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS papers_sha ON papers(sha256);
"""


def default_path() -> Path:
    env = os.environ.get("PFE_DB")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pfe" / "pfe.db"
    return Path.home() / ".local" / "share" / "pfe" / "pfe.db"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DB:
    """1 プロセス・複数スレッドから使う。書き込みはロックで直列にする。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(papers)")}
        if "requested_by" not in cols:      # 2026-10-30 以前の DB に列を足す
            self.con.execute("ALTER TABLE papers ADD COLUMN requested_by TEXT")
        self.lock = threading.Lock()

    # --- 読み ---
    def get(self, pid: str) -> dict | None:
        with self.lock:
            r = self.con.execute("SELECT * FROM papers WHERE pid=?", (pid,)).fetchone()
        return _row(r) if r else None

    def by_sha(self, sha: str) -> dict | None:
        with self.lock:
            r = self.con.execute("SELECT * FROM papers WHERE sha256=? ORDER BY created LIMIT 1", (sha,)).fetchone()
        return _row(r) if r else None

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.con.execute("SELECT * FROM papers ORDER BY created DESC").fetchall()
        return [_row(r) for r in rows]

    # --- 書き ---
    def upsert(self, pid: str, **fields) -> None:
        now = time.time()
        for k in ("figs", "done_figs", "failed_figs"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k] or [], ensure_ascii=False)
        if "authors" in fields and isinstance(fields["authors"], list):
            fields["authors"] = ", ".join(a for a in fields["authors"] if a)
        bad = set(fields) - set(COLUMNS)
        assert not bad, bad
        with self.lock:
            exists = self.con.execute("SELECT 1 FROM papers WHERE pid=?", (pid,)).fetchone()
            if exists:
                fields["updated"] = now
                sets = ", ".join(f"{k}=?" for k in fields)
                self.con.execute(f"UPDATE papers SET {sets} WHERE pid=?", (*fields.values(), pid))
            else:
                fields.setdefault("status", "queued")
                fields.setdefault("created", now)
                fields["updated"] = now
                cols = ["pid", *fields]
                self.con.execute(f"INSERT INTO papers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 (pid, *fields.values()))
            self.con.commit()

    def delete(self, pid: str) -> None:
        with self.lock:
            self.con.execute("DELETE FROM papers WHERE pid=?", (pid,))
            self.con.commit()

    # --- work/ から作り直す ---
    def scan_work(self, work_root: Path) -> int:
        """work/*/source.pdf のあるフォルダを一覧に入れる（もう一台で作ったもの・DB を消したあとの復元）。
        処理中・待ちの行は触らない。足した件数を返す。"""
        n = 0
        for d in sorted(p for p in work_root.iterdir() if p.is_dir() and (p / "source.pdf").exists()):
            row = self.get(d.name)
            if row and row["status"] not in ("done", "failed"):
                continue
            bundle = _read_json(d / "bundle.json") or {}
            # meta.json（無ければ bundle.json の paper）に、人が直した題を重ねる
            meta = paper_meta(d)
            figs = [f["id"] for f in (_read_json(d / "figures.json") or []) if (f["id"].startswith("fig") or f["id"].startswith("table") and f.get("clip"))]
            done = [f.get("id") for f in bundle.get("figures", []) if f.get("id")]
            fields = dict(sha256=row["sha256"] if row and row["sha256"] else sha256_file(d / "source.pdf"),
                          title=meta.get("title"), authors=meta.get("authors") or [], year=meta.get("year"),
                          doi=meta.get("doi"), journal=meta.get("journal"),
                          figs=figs, done_figs=done)
            if not row:
                fields.update(source=meta.get("source") or "", created=(d / "source.pdf").stat().st_mtime,
                              status="done" if (d / "player.html").exists() else "failed",
                              message=None if (d / "player.html").exists() else NO_PLAYER,
                              failed_figs=[])
                n += 1
            elif row["message"] == NO_PLAYER and (d / "player.html").exists():
                # 取り込んだ時点では作りかけだった本（run.py などが別に作っていた）。できたら棚に出す
                fields.update(status="done", message=None)
            self.upsert(d.name, **fields)
        return n


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("figs", "done_figs", "failed_figs"):
        d[k] = json.loads(d[k]) if d.get(k) else []
    return d


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
