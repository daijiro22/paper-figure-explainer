"""player の部品（player/parts/）を組み立てて 1 枚の HTML にする処理のテスト。"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "player"))
import build_player  # noqa: E402

PARTS = ROOT / "player" / "parts"


def test_assembled_template_has_every_part_and_no_marker():
    """差し込みの印が残らず、部品の中身がすべて入り、bundle の置き場は 1 つだけ。"""
    tpl = build_player.assemble_template()
    assert "/*@include" not in tpl
    for part in PARTS.glob("*"):
        if part.name != "shell.html":
            assert part.read_text(encoding="utf-8") in tpl, part.name
    assert len(build_player._SLOT.findall(tpl)) == 1


def test_parts_are_in_load_order():
    """読み込み時に実行される文があるので、順番が変わると壊れる（CAPTURE を使う前に Q を作る、など）。"""
    shell = (PARTS / "shell.html").read_text(encoding="utf-8")
    assert re.findall(r"^/\*@include ([\w.\-]+)\*/$", shell, re.MULTILINE) == [
        "player.css", "timeline.js", "stage.js", "overlay.js", "panel.js", "controls.js"]


def test_all_scripts_stay_in_one_script_tag():
    """部品は IIFE の変数を共有するので、<script> を分けてはいけない（bundle の置き場と本体の 2 つだけ）。"""
    tpl = build_player.assemble_template()
    assert len(re.findall(r"<script\b", tpl)) == 2
    assert "<script src" not in tpl and "<link" not in tpl


def test_build_embeds_bundle_in_a_single_html(tmp_path):
    """出来上がりは単一の HTML。bundle の値は壊れず、</script> が混ざっても閉じない。"""
    say = "字幕 <em>強調</em> </script> バックスラッシュ \\d"
    bundle = {"paper": {"title": "t"}, "figures": [{"id": "fig1", "img": "data:image/png;base64,AA==",
                                                     "steps": [{"say": say, "shapes": []}]}]}
    src = tmp_path / "bundle.json"
    src.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    out = build_player.build(src, tmp_path / "player.html")
    html = out.read_text(encoding="utf-8")
    payload = build_player._SLOT.search(html).group(2)
    assert "</script" not in payload
    assert json.loads(payload)["figures"][0]["steps"][0]["say"] == say
    assert "function stateAt" in html and "function buildShape" in html and "#panel" in html


@pytest.mark.parametrize("name,text", [("x.js", "var a = '</script>';\n"), ("x.css", "/* </style> */\n"),
                                       ("x.js", "// <!-- \n")])
def test_part_that_would_break_html_is_refused(tmp_path, monkeypatch, name, text):
    """埋め込むと <script> や <style> を閉じてしまう部品は、黙って組み立てずに止める。"""
    (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(build_player, "PARTS", tmp_path)
    with pytest.raises(RuntimeError):
        build_player._read_part(name)


def test_missing_part_is_refused(tmp_path, monkeypatch):
    shell = tmp_path / "shell.html"
    shell.write_text("<style>\n/*@include none.css*/\n</style>\n", encoding="utf-8")
    monkeypatch.setattr(build_player, "PARTS", tmp_path)
    monkeypatch.setattr(build_player, "TEMPLATE", shell)
    with pytest.raises(FileNotFoundError):
        build_player.assemble_template()


def test_marker_left_unresolved_is_refused(tmp_path, monkeypatch):
    """行頭に無い印は差し込めない。そのまま出さずに止める。"""
    shell = tmp_path / "shell.html"
    shell.write_text("<style>\n  /*@include a.css*/\n</style>\n", encoding="utf-8")
    monkeypatch.setattr(build_player, "TEMPLATE", shell)
    with pytest.raises(RuntimeError):
        build_player.assemble_template()


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い")
def test_timeline_runs_without_dom():
    """timeline.js は DOM に触れない。node だけで時間割と stateAt が動く。"""
    code = """
const vm = require('vm'), fs = require('fs');
const ctx = {console}; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const P = ctx.__player;
P.setBundle({overview: {steps: [{say: 'あ', duration: 2, shapes: []}]},
             figures: [{id: 'fig1', width: 1000, height: 600, takeaways_ja: ['ま'],
                        steps: [{say: 'い', duration: 4, shapes: [{kind: 'band', box: [10, 10, 50, 50]}]}]}]});
console.log(JSON.stringify({n: P.figures.length, over: P.figures[0].isOverview === true,
  starts: P.stepStarts(1), total: P.totalDuration(1), a: P.stateAt(1, 1.0), b: P.stateAt(1, 1.0),
  take: P.stateAt(1, 4.5).type}));
"""
    r = subprocess.run(["node", "-e", code, str(PARTS / "timeline.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert j["n"] == 2 and j["over"] is True                 # 概要が図の列の先頭に入る
    assert j["starts"] == [0] and j["take"] == "take"        # 図のステップのあとにまとめ
    assert j["a"] == j["b"] and j["a"]["shapes"][0]["shape"]["kind"] == "band"   # 同じ時刻なら同じ状態
