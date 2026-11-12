"""論文に自動でタグを付ける（施主 2026-11-10「追加されたら自動でつく」「タグがなければ新しく作って勝手にやっていいよ」）。

題・雑誌・著者・要旨を Claude API に読ませ、いまあるタグから選ばせる。合うものが無ければ新しいタグを作ってよい
（1 本につき新しいタグは 2 つまで。増えすぎないように、既存のタグを優先させる）。
1 本につき 1 回だけ走る（work/<pid>/autotag.json が目印。施主が後で外したタグを付け直さない）。
タグはもともと付いているものに足す（置き換えない）。鍵が無い・API が失敗したときは何もせずに戻る（論文の追加は止めない）。
呼び出し口: web/jobs.py（論文ができたとき）と scripts/autotag.py（まとめて付ける・付け直す）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pfe import tags as tagstore
from pfe.meta import paper_meta
from pfe.overview import TEXT_MODEL

MARK = "autotag.json"
MODEL = os.environ.get("PFE_AUTOTAG_MODEL") or TEXT_MODEL      # 文字だけの段なので Sonnet（pfe/overview.py）
MAX_TAGS = 4          # 1 本に付けるタグの上限
MAX_NEW = 2           # 1 本で新しく作ってよいタグの数
ABSTRACT_CHARS = 3000


def _abstract(d: Path) -> str:
    """要旨（abstract.json）。無ければ本文の頭（text.json）を少しだけ。"""
    for name in ("abstract.json", "text.json"):
        p = d / name
        if not p.exists():
            continue
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip():
            return v["text"][:ABSTRACT_CHARS]
        if isinstance(v, dict) and isinstance(v.get("paragraphs"), list):     # text.json は段落の並び
            s = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in v["paragraphs"][:6])
            if s.strip():
                return s[:ABSTRACT_CHARS]
    return ""


def _counts(work_root: Path, live: dict[str, dict]) -> dict[str, int]:
    """タグごとの付いている本数（どのタグがよく使われているかを LLM に見せる）。"""
    n = {tid: 0 for tid in live}
    for d in work_root.iterdir():
        if d.is_dir() and not d.name.startswith("_"):
            for tid in tagstore.paper_tags(d):
                if tid in n:
                    n[tid] += 1
    return n


def build_prompt(meta: dict, abstract: str, existing: list[tuple[str, int]]) -> str:
    tag_lines = "\n".join(f"- {name}（{cnt} 本）" for name, cnt in existing) or "（まだ無い）"
    authors = ", ".join(meta.get("authors") or [])[:400]
    return f"""研究室の論文棚で、論文を分けるためのタグを選んでください。棚は木質バイオマス・リグニン・セルロースの化学の論文が中心です。

## いまあるタグ（付いている本数）
{tag_lines}

## 論文
- 題: {meta.get("title") or ""}
- 雑誌: {meta.get("journal") or ""}（{meta.get("year") or ""}）
- 著者: {authors}
- 要旨:
{abstract or "（読めなかった）"}

## 決まり
- この論文の主題・手法・対象を表すタグを 1〜{MAX_TAGS} 個。いまあるタグで合うものを最優先で選ぶ（名前は一字一句そのまま）。
- 合うものが無いときだけ、新しいタグを {MAX_NEW} 個まで作ってよい。いまあるタグと同じ粒度の短い日本語（化合物名・手法名の略号は英字のままでよい）。10 字程度まで。「論文」「研究」「化学」のような広すぎる語や、この 1 本にしか使えない細かすぎる語は作らない。
- 研究室・人の名前のタグ（所属を表すもの）は、著者にその人がいるときだけ選ぶ。新しく作らない。
- 迷うものは付けない（少なめでよい）。

## 答え
次の形の JSON オブジェクトを 1 つだけ返す。``` で囲まず、前後に説明を書かない。
{{"existing": ["いまあるタグの名前", ...], "new": ["新しいタグの名前", ...], "reason": "選んだ理由を 1 文"}}"""


def _call(prompt: str) -> dict:
    """Claude API を呼んで JSON を返す。鍵は ANTHROPIC_API_KEY か ~/.config/pfe/env（pfe.analyze と同じ）。"""
    import anthropic
    from pfe.analyze import load_env_file
    load_env_file()
    client = anthropic.Anthropic(timeout=120, max_retries=2)
    msg = client.messages.create(model=MODEL, max_tokens=2000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if "{" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    v = json.loads(text)
    if not isinstance(v, dict):
        raise ValueError("答えが JSON オブジェクトでない")
    v["_model"] = msg.model
    v["_usage"] = {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens}
    return v


def pick(answer: dict, live_by_name: dict[str, str]) -> tuple[list[str], list[str]]:
    """LLM の答えから、付ける既存タグの名前と新しく作るタグの名前を決める（上限と重複をここで守る）。"""
    def names(key: str) -> list[str]:
        v = answer.get(key) or []
        return [" ".join(str(x).split()) for x in v if isinstance(x, str) and str(x).strip()]
    use = [n for n in names("existing") if n in live_by_name]
    new = []
    for n in names("new"):
        if n in live_by_name:              # 既存の名前を new に入れてきたときは既存として扱う
            use.append(n)
        elif len(n) <= 20 and n not in new:
            new.append(n)
    use = list(dict.fromkeys(use))[:MAX_TAGS]
    new = new[:max(0, min(MAX_NEW, MAX_TAGS - len(use)))]
    return use, new


def auto_tag(d: Path, work_root: Path, force: bool = False, dry_run: bool = False, call=_call) -> dict | None:
    """論文 d にタグを付ける。付けたら結果（autotag.json の中身）を返す。走らなかったら None。"""
    if (d / MARK).exists() and not force:
        return None
    meta = paper_meta(d)
    abstract = _abstract(d)
    if not (meta.get("title") or abstract):
        return None
    live = {t["id"]: t for t in tagstore.active(work_root)}
    cnt = _counts(work_root, live)
    existing = sorted(((t["name"], cnt[tid]) for tid, t in live.items()), key=lambda x: -x[1])
    answer = call(build_prompt(meta, abstract, existing))
    by_name = {t["name"]: tid for tid, t in live.items()}
    use, new = pick(answer, by_name)
    result = {"at": round(time.time()), "model": answer.get("_model"), "usage": answer.get("_usage"),
              "existing": use, "new": new, "reason": answer.get("reason")}
    if dry_run:
        return result
    ids = [by_name[n] for n in use]
    for n in new:
        ids.append(tagstore.create(work_root, n)["id"])
    live = {t["id"] for t in tagstore.active(work_root)}
    now = [t for t in tagstore.paper_tags(d) if t in live]
    tagstore.set_paper_tags(d, now + [i for i in ids if i not in now], live)
    (d / MARK).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
