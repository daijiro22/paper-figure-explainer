"""受け付けた論文を 1 本ずつ処理する（Codex は 1 本ずつ・並列禁止なので、worker は 1 つだけ）。

流れ: アップされた PDF → extract_text.py で図の一覧 → run.py を図 1 枚ずつ。
（DOI・URL からの取得は外した。施主 2026-10-30「DOI は無しで PDF だけにしよう」。コマンドの run.py --url は残す）
run.py は --figs に入れた図だけで player.html を作り直すので、毎回「できた図＋今回の図」を渡す。
できた図の解析は使い回されるので作り直しは数秒で、図が 1 枚できるたびに player.html で見られる。
パイプライン（pfe/・run.py）には手を入れず、外から子プロセスで呼ぶ。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

from pfe.meta import paper_meta
from web.db import DB, sha256_file

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
RUN = ROOT / "scripts" / "run.py"
EXTRACT = ROOT / "scripts" / "extract_text.py"
SEC_PER_FIG = 150          # 1 図 2〜3 分（実測）。待ち時間の目安に使う
MAX_UPLOAD = 100 << 20     # 100 MB
ACTIVE = ("queued", "extracting", "running")


class InputError(ValueError):
    """利用者の入力の誤り（画面にそのまま出す）。"""


def paper_id_from(name: str) -> str:
    """ファイル名から短い英数字の id を作る（scripts/run.py の paper_id_from と同じ規則）。"""
    stem = Path(name).stem
    m = re.match(r"\s*([A-Za-z]+).*?(\d{4})", stem)
    words = re.findall(r"[A-Za-z]{4,}", stem)
    tail = words[3].lower() if len(words) > 3 else "paper"
    if m:
        return f"{m.group(1).lower()}{m.group(2)}_{tail}"
    return re.sub(r"\W+", "_", stem.lower()).strip("_")[:30] or "paper"


class Jobs:
    def __init__(self, db: DB, driver: str = "codex", python: str = sys.executable):
        self.db = db
        self.driver = driver
        self.python = python
        self.q: queue.Queue[str] = queue.Queue()
        self.no_tts: dict[str, bool] = {}

    # ---- 受付 ----
    def start(self) -> None:
        """work/ を一覧に取り込み、止まっていた分を待ち行列に戻して worker を起こす。"""
        WORK.mkdir(exist_ok=True)
        self.db.scan_work(WORK)
        for row in reversed(self.db.list()):
            if row["status"] in ACTIVE:
                if not (WORK / row["pid"] / "source.pdf").exists():
                    self.db.upsert(row["pid"], status="failed", message="PDF が見つからない")
                    continue
                self.db.upsert(row["pid"], status="queued", current=None)
                self.q.put(row["pid"])
        threading.Thread(target=self._loop, daemon=True, name="pfe-worker").start()

    def submit_pdf(self, data: bytes, filename: str, figs: list[str] | None, no_tts: bool = False,
                   by: str | None = None) -> str:
        if not data.startswith(b"%PDF-"):
            raise InputError("PDF ではないファイルです")
        if len(data) > MAX_UPLOAD:
            raise InputError("100 MB を超える PDF は受け付けません")
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        old = self.db.by_sha(sha)
        if old:                                       # 同じ PDF は作り直さない（足りない図だけ足す）
            return self._reuse(old, figs, no_tts)
        name = Path(filename.replace("\\", "/")).name or "paper.pdf"
        pid = paper_id_from(name)
        d = WORK / pid
        if self.db.get(pid) or (d / "source.pdf").exists():
            # 同じ名前の別の論文: hash の頭を付けて分ける（同じ PDF なら上の by_sha で拾われている）
            if (d / "source.pdf").exists() and sha256_file(d / "source.pdf") == sha:
                self.db.scan_work(WORK)
                return self._reuse(self.db.get(pid), figs, no_tts)
            pid = f"{pid}_{sha[:6]}"
            d = WORK / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.pdf").write_bytes(data)
        self.db.upsert(pid, sha256=sha, source=name, status="queued", figs=figs or [],
                       done_figs=[], failed_figs=[], message=None, requested_by=by)
        self.no_tts[pid] = no_tts
        self.q.put(pid)
        return pid

    def retry(self, pid: str) -> None:
        row = self.db.get(pid)
        if not row:
            raise InputError("その論文は一覧に無い")
        if row["status"] in ACTIVE:
            return
        if not (WORK / pid / "source.pdf").exists():
            raise InputError("PDF が無いのでやり直せません。PDF をアップし直してください")
        self.db.upsert(pid, status="queued", failed_figs=[], message=None)
        self.q.put(pid)

    def _reuse(self, row: dict, figs: list[str] | None, no_tts: bool) -> str:
        """作成済みの論文: 頼まれた図が全部あればそのまま返し、足りなければ足りない図だけ作る。"""
        pid = row["pid"]
        if row["status"] in ACTIVE:
            return pid
        want = figs or row["figs"]
        if row["status"] == "done" and want and set(want) <= set(row["done_figs"]):
            return pid
        self.db.upsert(pid, status="queued", figs=figs or [], failed_figs=[], message=None)
        self.no_tts[pid] = no_tts
        self.q.put(pid)
        return pid

    # ---- 処理 ----
    def _loop(self) -> None:
        while True:
            pid = self.q.get()
            try:
                self._process(pid)
            except Exception as e:  # worker は止めない（次の論文へ）
                self.db.upsert(pid, status="failed", current=None, message=f"内部エラー: {e!r}")

    def _process(self, pid: str) -> None:
        row = self.db.get(pid)
        if not row or row["status"] != "queued":
            return
        d = WORK / pid
        log = d / "web.log"

        if not (d / "source.pdf").exists():
            self.db.upsert(pid, status="failed", message="PDF が見つからない")
            return

        # 1. 図の一覧と論文の情報（数秒）
        if not (d / "figures.json").exists() or not (d / "meta.json").exists():
            self.db.upsert(pid, status="extracting")
            rc, tail = self._run([str(EXTRACT), "--pdf", str(d / "source.pdf"), "--work", str(d)], log)
            if rc != 0:
                self.db.upsert(pid, status="failed", message=f"図・本文の読み取りに失敗: {tail}")
                return
        meta = paper_meta(d)                      # 人が直した題があればそれを使う
        order = [f["id"] for f in json.loads((d / "figures.json").read_text(encoding="utf-8"))
                 if (f["id"].startswith("fig") or f["id"].startswith("table") and f.get("clip"))]   # 図と表
        self.db.upsert(pid, title=meta.get("title"), authors=meta.get("authors") or [], year=meta.get("year"),
                       doi=meta.get("doi") or row["doi"], journal=meta.get("journal"))
        targets = [f for f in order if f in set(row["figs"])] if row["figs"] else order
        if not targets:
            self.db.upsert(pid, status="failed", figs=order, message="図が見つかりませんでした"
                           + (f"（この PDF の図は {', '.join(order)}）" if order else "（キャプションを読めなかった）"))
            return
        # 前に作った図も player から消さない（run.py は --figs の図だけで作り直すため）
        had = self._bundle_ids(d)
        done = [f for f in order if f in had]
        # figs は「今回の図＋前に作った図」（前の図を数えないと「3/1 図」になる）
        self.db.upsert(pid, status="running", figs=[f for f in order if f in set(targets) | set(done)],
                       done_figs=done, failed_figs=[])

        # 2. 図を 1 枚ずつ（できた図＋今回の図で player.html を作り直す）
        failed: list[str] = []
        for fid in targets:
            if fid in done:
                continue
            self.db.upsert(pid, current=fid)
            cum = [f for f in order if f in set(done) | {fid}]
            cmd = [str(RUN), "--pdf", str(d / "source.pdf"), "--id", pid, "--figs", ",".join(cum),
                   "--driver", self.driver]
            if self.no_tts.get(pid):
                cmd.append("--no-tts")
            rc, tail = self._run(cmd, log)
            if rc != 0:
                self.db.upsert(pid, status="failed", current=None, failed_figs=failed + [fid],
                               message=f"{fid} の処理中に止まりました: {tail}")
                return
            if fid in self._bundle_ids(d):
                done.append(fid)
            else:
                failed.append(fid)
            self.db.upsert(pid, done_figs=[f for f in order if f in set(done)], failed_figs=failed)

        msg = f"{len(failed)} 図は解析できませんでした（{', '.join(failed)}）" if failed else None
        self.db.upsert(pid, status="done", current=None, message=msg)

    @staticmethod
    def _bundle_ids(d: Path) -> list[str]:
        try:
            b = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
            return [f["id"] for f in b.get("figures", [])]
        except (OSError, json.JSONDecodeError, KeyError):
            return []

    def _run(self, args: list[str], log: Path) -> tuple[int, str]:
        """子プロセスで python スクリプトを動かし、出力を web.log に足す。(終了コード, 最後の数行) を返す。"""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        lines: list[str] = []
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(Path(a).name if i == 0 else a for i, a in enumerate(args))}\n")
            p = subprocess.Popen([self.python, *args], cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            for line in p.stdout:
                f.write(line)
                f.flush()
                lines.append(line.rstrip())
                lines = lines[-8:]
            rc = p.wait()
        return rc, " / ".join(l for l in lines[-3:] if l)

    # ---- 画面用 ----
    def view(self, row: dict) -> dict:
        """一覧・詳細に出す形（待ち時間の目安と player の場所を足す）。"""
        out = dict(row)
        left = [f for f in row["figs"] if f not in row["done_figs"] and f not in row["failed_figs"]]
        out["eta_sec"] = len(left) * SEC_PER_FIG if row["status"] in ("queued", "running") else 0
        out["player"] = (f"/p/{row['pid']}"
                         if (WORK / row["pid"] / "player.html").exists() and row["done_figs"] else None)
        out["queue_pos"] = None
        return out
