"""論文のタグ（施主 2026-10-30「名前をつけて分けたい。タグで作って」。Codex と相談してフォルダではなくタグ）。

保存の分け方（2 台の PC で同期しても壊れにくいように）:
  - work/tags.json           タグの一覧。{"tags": [{"id", "name", "deleted", "created"}]}
                             名前を変えてもここだけ書き換えれば全部の論文に反映される（論文側は id で持つ）
  - work/<pid>/meta_override.json の "tags"   その論文に付いたタグの id の並び
タグを消すときは deleted を立てるだけ（記録を残す）。古い PC から同期で論文側の id が戻ってきても、
消したタグは表示・絞り込み・件数に出さない。同じ名前で作り直すと新しい id になる。
論文側に、一覧に無い id（まだ同期が届いていないタグ）があっても消さずに残す。
研究室サイト（scripts/export_site.py）の棚にも名前を出す（施主 2026-11-06「ねっとのものにも」）。編集は /make だけ。
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from pfe.meta import read_override, write_override

FILE = "tags.json"


def _path(work_root: Path) -> Path:
    return work_root / FILE


def read_all(work_root: Path) -> list[dict]:
    """消したものも含むタグの一覧（作った順）。ファイルがあるのに読めなければ例外（空で上書きしないため）。"""
    p = _path(work_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{FILE} が読めない（同期の途中かもしれない）: {e}")
    return [t for t in data.get("tags", []) if isinstance(t, dict) and t.get("id")]


def active(work_root: Path) -> list[dict]:
    return [t for t in read_all(work_root) if not t.get("deleted")]


def _write(work_root: Path, tags: list[dict]) -> None:
    p = _path(work_root)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tags": tags}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not n:
        raise ValueError("タグの名前が空です")
    if len(n) > 40:
        raise ValueError("タグの名前が長すぎます（40 字まで）")
    return n


def create(work_root: Path, name: str) -> dict:
    """タグを作る。同じ名前の（消していない）タグがあればそれを返す。"""
    n = _clean_name(name)
    tags = read_all(work_root)
    for t in tags:
        if not t.get("deleted") and t["name"] == n:
            return t
    t = {"id": "t" + secrets.token_hex(4), "name": n, "deleted": False, "created": round(time.time())}
    _write(work_root, tags + [t])
    return t


def update(work_root: Path, tid: str, name: str | None = None, deleted: bool | None = None) -> dict:
    """名前を変える・消す。"""
    tags = read_all(work_root)
    hit = next((t for t in tags if t["id"] == tid), None)
    if hit is None:
        raise KeyError(tid)
    if name is not None:
        n = _clean_name(name)
        if any(t is not hit and not t.get("deleted") and t["name"] == n for t in tags):
            raise ValueError(f"「{n}」というタグはもうあります")
        hit["name"] = n
    if deleted is not None:
        hit["deleted"] = bool(deleted)
    _write(work_root, tags)
    return hit


def paper_tags(d: Path) -> list[str]:
    """論文に付いたタグの id（一覧に無い id も含めてそのまま）。"""
    v = read_override(d).get("tags") or []
    return [x for x in v if isinstance(x, str)]


def set_paper_tags(d: Path, ids: list[str], known: set[str]) -> list[str]:
    """論文のタグを ids にする。known（一覧にある id）以外を新しく付けることはできない。
    いま付いていて一覧に無い id（まだ同期が届いていないタグ）は残す。"""
    keep_unknown = [x for x in paper_tags(d) if x not in known]
    bad = [x for x in ids if x not in known]
    if bad:
        raise ValueError(f"無いタグ: {bad}")
    out = list(dict.fromkeys(list(ids) + keep_unknown))
    write_override(d, tags=out or None)
    return out


def visible(d: Path, live: dict[str, dict]) -> list[dict]:
    """論文に付いたタグのうち、表示するもの（消していない・一覧にある）。"""
    return [{"id": i, "name": live[i]["name"]} for i in paper_tags(d) if i in live]
