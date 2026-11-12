"""自動タグ付け（pfe/autotag.py）。API は呼ばず、答えを差し替えて確かめる。"""
import json

from pfe import autotag as A
from pfe import tags as T


def _paper(work, pid, title="Alkaline delignification of wood", abstract="Lignin was removed by NaOH."):
    d = work / pid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"title": title, "authors": ["A B"]}), encoding="utf-8")
    (d / "abstract.json").write_text(json.dumps({"text": abstract}), encoding="utf-8")
    return d


def test_picks_existing_and_creates_new_once(tmp_path):
    work = tmp_path
    old = T.create(work, "脱リグニン")
    d = _paper(work, "p1")
    seen = {}

    def fake(prompt):
        seen["prompt"] = prompt
        return {"existing": ["脱リグニン", "無いタグ"], "new": ["アルカリ処理", "脱リグニン"], "reason": "x"}

    r = A.auto_tag(d, work, call=fake)
    assert "脱リグニン（0 本）" in seen["prompt"] and "Lignin was removed" in seen["prompt"]
    assert r["existing"] == ["脱リグニン"] and r["new"] == ["アルカリ処理"]
    names = {t["id"]: t["name"] for t in T.active(work)}
    assert [names[i] for i in T.paper_tags(d)] == ["脱リグニン", "アルカリ処理"]
    assert T.paper_tags(d)[0] == old["id"]
    # 2 回目は走らない（施主が外したタグを付け直さない）
    assert A.auto_tag(d, work, call=lambda p: 1 / 0) is None


def test_keeps_tags_already_on_paper_and_limits_new(tmp_path):
    work = tmp_path
    mine = T.create(work, "河川")
    d = _paper(work, "p2")
    T.set_paper_tags(d, [mine["id"]], {mine["id"]})
    r = A.auto_tag(d, work, call=lambda p: {"existing": [], "new": ["a", "b", "c"]})
    assert r["new"] == ["a", "b"]                       # 新しいタグは 2 つまで
    assert T.paper_tags(d)[0] == mine["id"] and len(T.paper_tags(d)) == 3


def test_dry_run_writes_nothing(tmp_path):
    work = tmp_path
    d = _paper(work, "p3")
    r = A.auto_tag(d, work, dry_run=True, call=lambda p: {"existing": [], "new": ["熱分解"]})
    assert r["new"] == ["熱分解"]
    assert T.active(work) == [] and T.paper_tags(d) == [] and not (d / A.MARK).exists()
