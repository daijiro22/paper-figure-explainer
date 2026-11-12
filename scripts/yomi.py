"""読み間違いの表（pfe/yomi.tsv）を育てる道具。声は edge-tts のまま、読みだけ直す（施主 2026-11-08）。

    py -3.13 scripts/yomi.py scan [--top 40]          # まだ確かめていない英字・記号の塊を多い順に出す
    py -3.13 scripts/yomi.py sheet [--top 30]         # 例文の声つきの確認シート → work/_yomi/sheet.html
    py -3.13 scripts/yomi.py revoice [<pid>...]       # 表に合わせて作り直す声の数を見る（書き換えない）
    py -3.13 scripts/yomi.py revoice [<pid>...] --apply   # 声を作り直し、bundle.json と player.html を組み直す

回し方: sheet を開いて聞く → 「正しい」「間違い＋読み」を付ける → 下の欄の行を pfe/yomi.tsv に足す
→ revoice で件数を見る → --apply。漢字の読み違いは候補に出ないので、聞いて気づいたら同じ表に足す。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import datetime
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from pfe.yomi import TABLE, candidates, to_speech  # noqa: E402

WORK = ROOT / "work"
OUT = WORK / "_yomi"


def bundle_dirs(names: list[str]) -> list[Path]:
    """対象の論文フォルダ（名前の指定が無ければ bundle.json のある全部）。"""
    if names:
        return [WORK / n for n in names]
    return sorted(p.parent for p in WORK.glob("*/bundle.json"))


def bundle_steps(b: dict) -> list[dict]:
    """bundle の全ステップ（図の解説と概要）。"""
    steps = [st for f in b.get("figures") or [] for st in f.get("steps") or []]
    ov = b.get("overview")
    if isinstance(ov, dict):
        steps += ov.get("steps") or []
    return steps


def all_says(names: list[str]) -> list[str]:
    says = []
    for d in bundle_dirs(names):
        try:
            b = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        says += [st.get("say") or "" for st in bundle_steps(b)]
    return says


# ---- scan ----
def cmd_scan(args) -> int:
    cands = candidates(all_says(args.names))
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["塊\t回数\t表記の例\t例文"] + [f"{c['key']}\t{c['count']}\t{c['form']}\t{c['example']}" for c in cands]
    (OUT / "candidates.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"まだ確かめていない塊: {len(cands)} 種類（全部は {OUT / 'candidates.tsv'}）")
    for c in cands[:args.top]:
        print(f"{c['count']:5d}  {c['form']:<14}  {c['example'][:60]}")
    return 0


# ---- sheet ----
SHEET_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#dedbd2;--mark:#fff1a8;--accent:#b4372b}
@media (prefers-color-scheme: dark){:root{--bg:#1b1b1a;--fg:#ecebe6;--muted:#a3a29c;--line:#3a3935;--mark:#6b5b12;--accent:#f08a7e}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.6 "Yu Gothic UI","Hiragino Sans",sans-serif}
h1{font-size:20px;margin:0 0 4px} p.note{color:var(--muted);margin:0 0 16px}
table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid var(--line);padding:8px 6px;vertical-align:top;text-align:left}
th{font-size:13px;color:var(--muted);font-weight:600} mark{background:var(--mark);color:inherit}
input[type=text]{width:9em;font:inherit;padding:2px 4px} audio{height:32px;width:220px}
.ex{max-width:34em} .n{color:var(--muted);font-size:13px} label{white-space:nowrap;margin-right:8px}
textarea{width:100%;height:12em;font:13px/1.5 Consolas,monospace;margin-top:8px}
button{font:inherit;padding:4px 14px;margin-top:8px}
@media (max-width:700px){thead{display:none} td,th{display:block;border:0;padding:2px 0} tr{display:block;border-bottom:1px solid var(--line);padding:8px 0}}
"""

SHEET_JS = """
var KEY='pfe-yomi-sheet';
function rows(){return Array.prototype.slice.call(document.querySelectorAll('tr[data-i]'));}
function collect(){
  var today=document.body.dataset.today, out=[], state={};
  rows().forEach(function(tr){
    var i=tr.dataset.i, s=tr.querySelector('.s').value.trim(), r=tr.querySelector('.r').value.trim();
    var v=(tr.querySelector('input[type=radio]:checked')||{}).value||'';
    state[i]={s:s,r:r,v:v};
    if(!s||!v) return;
    if(v==='ok') out.push(s+'\\t=\\t'+today+' シートで正しいと確認');
    else if(r) out.push(s+'\\t'+r+'\\t'+today+' シートで確認');
    else out.push('# '+s+'\\t（読みを書いてください）');
  });
  document.getElementById('out').value=out.join('\\n');
  try{localStorage.setItem(KEY,JSON.stringify(state));}catch(e){}
}
function restore(){
  var state={}; try{state=JSON.parse(localStorage.getItem(KEY)||'{}');}catch(e){}
  rows().forEach(function(tr){
    var st=state[tr.dataset.i]; if(!st) return;
    if(st.s) tr.querySelector('.s').value=st.s;
    tr.querySelector('.r').value=st.r||'';
    var rb=tr.querySelector('input[value="'+st.v+'"]'); if(rb) rb.checked=true;
  });
}
document.addEventListener('input',collect); document.addEventListener('change',collect);
document.getElementById('copy').onclick=function(){
  var t=document.getElementById('out'); t.select();
  try{navigator.clipboard.writeText(t.value);}catch(e){document.execCommand('copy');}
};
restore(); collect();
"""


def _highlight(sentence: str, form: str) -> str:
    """例文の中の塊に黄色を付ける（HTML の文字はエスケープしてから）。"""
    esc, f = html.escape(sentence), html.escape(form)
    return esc.replace(f, f"<mark>{f}</mark>", 1) if f else esc


def cmd_sheet(args) -> int:
    from pfe.tts import synth
    cands = candidates(all_says(args.names))[:args.top]
    if not cands:
        print("確かめる候補はありません")
        return 0
    audio_dir = OUT / "audio"
    body = []
    for i, c in enumerate(cands):
        # 例文はすでに表で置き換えた形なので、もう一度は置き換えない
        p, _ = synth(c["example"], audio_dir, apply_yomi=False)
        name = f"r{i}"
        body.append(
            f'<tr data-i="{i}"><td class="n">{i + 1}<br>{c["count"]} 回</td>'
            f'<td><input type="text" class="s" value="{html.escape(c["form"], quote=True)}"></td>'
            f'<td class="ex">{_highlight(c["example"], c["form"])}<br><audio controls preload="none" src="audio/{p.name}"></audio></td>'
            f'<td><label><input type="radio" name="{name}" value="ok">正しい</label>'
            f'<label><input type="radio" name="{name}" value="ng">間違い</label></td>'
            f'<td><input type="text" class="r" placeholder="正しい読み"></td></tr>')
        print(f"  {i + 1:3d}. {c['form']}（{c['count']} 回）")
    today = datetime.date.today().isoformat()
    page = (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>読み確認シート</title>'
        f'<style>{SHEET_CSS}</style></head><body data-today="{today}">'
        '<h1>読み確認シート</h1>'
        f'<p class="note">ナレーションに多い英字・記号を {len(cands)} 個、多い順に並べました。例文を聞いて「正しい」か「間違い」を選び、'
        '間違いなら正しい読みをカタカナかひらがなで書いてください。表記の欄は直せます。'
        '数字のあとの単位だけ直すときは、表記を「{n} K」、読みを「{n}ケルビン」のように書きます（図の札の K には当たらなくなります）。'
        '一番下の欄の行を pfe/yomi.tsv に足すか、チャットに貼ってください。</p>'
        '<table><thead><tr><th>#</th><th>表記</th><th>例文（声に渡す形）</th><th>判定</th><th>読み</th></tr></thead><tbody>'
        + "".join(body) +
        '</tbody></table><h2 style="font-size:16px;margin-top:24px">pfe/yomi.tsv に足す行</h2>'
        '<textarea id="out" readonly></textarea><br><button id="copy">コピー</button>'
        f'<script>{SHEET_JS}</script></body></html>')
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sheet.html").write_text(page, encoding="utf-8")
    print(f"確認シート: {OUT / 'sheet.html'}")
    return 0


# ---- revoice ----
def cmd_revoice(args) -> int:
    from pfe.tts import audio_name, synth
    changed_dirs, n_steps = [], 0
    for d in bundle_dirs(args.names):
        bpath = d / "bundle.json"
        if not bpath.exists():
            print(f"bundle.json が無い: {d.name}")
            continue
        b = json.loads(bpath.read_text(encoding="utf-8"))
        # 今の表で作るはずの声の名前と、bundle に入っている声の名前が違うステップだけ作り直す
        todo = [st for st in bundle_steps(b)
                if st.get("audio") and st.get("say")
                and st["audio"] != "audio/" + audio_name(to_speech(st["say"]))]
        if not todo:
            continue
        n_steps += len(todo)
        changed_dirs.append(d.name)
        print(f"  {d.name}: {len(todo)} ステップ")
        if not args.apply:
            continue
        for st in todo:
            p, dur = synth(st["say"], d / "audio")       # 古い mp3 は消さない（もう一台の PC の player が使っていることがある）
            st["audio"], st["duration"] = f"audio/{p.name}", dur
        bpath.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"作り直す声: {len(changed_dirs)} 本・{n_steps} ステップ" + ("" if args.apply else "（--apply で作り直す）"))
    if args.apply and changed_dirs:
        import rebuild_players
        rebuild_players.main(changed_dirs)       # player.html を組み直し、もう一台へ知らせる
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="読み間違いの表（pfe/yomi.tsv）を育てる")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, top in (("scan", 40), ("sheet", 30)):
        sp = sub.add_parser(name)
        sp.add_argument("names", nargs="*", help="論文フォルダ名（省略で全部）")
        sp.add_argument("--top", type=int, default=top)
    sp = sub.add_parser("revoice")
    sp.add_argument("names", nargs="*")
    sp.add_argument("--apply", action="store_true", help="声を作り直して bundle.json と player.html を書き換える")
    args = ap.parse_args(argv)
    print(f"読みの表: {TABLE}")
    return {"scan": cmd_scan, "sheet": cmd_sheet, "revoice": cmd_revoice}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
