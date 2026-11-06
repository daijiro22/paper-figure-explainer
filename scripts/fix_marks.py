"""注記の判定（scripts/judge_marks.py）で旗の立った図を、Jev の指摘を添えて Claude API に解析し直させる。

    python scripts/fix_marks.py sapouna2021_lignin --figs fig8          # 1 図だけ試す
    python scripts/fix_marks.py --all --jobs 3                          # 旗の立った図を全部（論文を 3 本ずつ並行）
    python scripts/fix_marks.py --all --dry-run                         # 対象と指摘文だけ出す

流れ（論文ごと）:
  1. 旗の立った図ごとに fig/<id>.feedback.md（指摘と前回の答え）を置き、今の答えを <id>.analysis.prejev.json に控える
  2. run.py --driver api を回す（答えの無い図だけ解析し直し、声と player も作り直す）
  3. judge_marks.py で判定し直し、旗の立った注記が**減った図だけ**新しい答えを残す。減らなければ控えに戻して player を組み直す
結果は work/_fix_marks.jsonl に 1 図 1 行で追記する。施主 2026-10-31「API で自動で作り直す」。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import judge_marks as JM  # noqa: E402

WORK = ROOT / "work"
KIND_JA = {"box": "四角", "circle": "丸", "band": "帯", "axis": "軸", "arrow": "矢印", "trace": "曲線のなぞり"}


def flagged(d: Path) -> dict[str, list[tuple[int, dict]]]:
    """{図: [(ステップ番号, 判定), ...]}（旗の立ったステップだけ・今のしきい値で）。"""
    p = d / "judge_marks.json"
    if not p.exists():
        return {}
    out: dict[str, list] = {}
    for sid, r in json.loads(p.read_text(encoding="utf-8")).get("steps", {}).items():
        r = JM.reflag(r)
        if r.get("flags"):
            fid, si = sid.split("#")
            out.setdefault(fid, []).append((int(si), r))
    return {k: sorted(v, key=lambda x: x[0]) for k, v in out.items()}


def n_flags(d: Path, fid: str) -> int:
    """その図で旗の立った注記の数。"""
    return sum(len(r["flags"]) for _, r in flagged(d).get(fid, []))


def feedback_text(fig: dict, items: list[tuple[int, dict]], analysis: dict) -> str:
    """Claude に渡す指摘（日本語）。注記は描いた順の番号（寄りを除く）と種類・札で示す。"""
    lines = ["## 別のモデル（注記の判定）からの指摘",
             "前回の答えを図に描いて別のモデルに見せたところ、次のステップの注記が「ナレーションの話している物を指していない」"
             "または「何も無い所・文字の切れ端を囲っている」と判定されました。",
             "番号札の画像を見直し、ナレーションが話している物（文字・ピーク・表の行やセル・曲線など）をそのまま指す番号"
             "（番号が無いときは box_norm）を選び直してください。注記の数は話している物の数に合わせ、話に出ない物は囲まないこと。",
             "指摘が誤りだと判断できる注記は変えなくてかまいません。指摘の無いステップは、言葉・注記・根拠を前回のまま返してください。",
             "全体を JSON で返し直してください。", ""]
    for si, r in items:
        st = fig["steps"][si]
        marks = JM.marks_of(st)
        lines.append(f"- ステップ {si + 1}（{len(fig['steps'])} 中）「{st.get('say', '')}」")
        for f in r["flags"]:
            m, kind = f.split(":")
            i = int(m[1:]) - 1
            mk = marks[i] if i < len(marks) else {}
            desc = KIND_JA.get(mk.get("kind"), mk.get("kind") or "?") + (f"・札「{mk['label']}」" if mk.get("label") else "")
            if kind == "empty":
                lines.append(f"    - {i + 1} 番目の注記（{desc}）: 何も無い所か文字の切れ端を囲っている"
                             f"（確率 {r['p'].get(m + '_empty')}）")
            else:
                lines.append(f"    - {i + 1} 番目の注記（{desc}）: ナレーションの話している物を指していない"
                             f"（指している確率 {r['p'].get(m + '_relevant')}）")
    lines += ["", "前回の答え:", json.dumps(analysis, ensure_ascii=False)]
    return "\n".join(lines)


def _history() -> list[dict]:
    p = WORK / "_fix_marks.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def run(cmd: list[str], log: Path) -> int:
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(cmd)}  ({time.strftime('%H:%M:%S')})\n")
        f.flush()
        return subprocess.call(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)


def fix_paper(d: Path, args) -> list[dict]:
    """1 本の論文の旗の立った図を作り直し、図ごとの結果を返す。"""
    bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
    figs = {f["id"]: f for f in bundle["figures"]}
    targets = flagged(d)
    if args.figs:
        targets = {k: v for k, v in targets.items() if k in set(args.figs.split(","))}
    targets = {k: v for k, v in targets.items() if k in figs}
    if not args.retry:                     # 一度作り直した図（戻した図も）は頼み直さない（費用が際限なく増えないように）
        done = {(r["paper"], r["fig"]) for r in _history()}
        targets = {k: v for k, v in targets.items() if (d.name, k) not in done}
    if not targets:
        return []
    fdir, log = d / "fig", d / "fix_marks.log"
    before = {fid: n_flags(d, fid) for fid in targets}
    for fid, items in targets.items():
        apath = fdir / f"{fid}.analysis.json"
        text = feedback_text(figs[fid], items, json.loads(apath.read_text(encoding="utf-8")))
        if args.dry_run:
            print(f"--- {d.name}/{fid}（旗 {before[fid]}）\n" + text.split("前回の答え:")[0])
            continue
        (fdir / f"{fid}.feedback.md").write_text(text, encoding="utf-8")
        shutil.copyfile(apath, fdir / f"{fid}.analysis.prejev.json")
        apath.unlink()
        for suf in (".analysis.raw.json", ".analysis.raw.sig"):     # 前回の生の答えを回収させない
            (fdir / f"{fid}{suf}").unlink(missing_ok=True)
    if args.dry_run:
        return []

    py = sys.executable
    run([py, "scripts/run.py", "--pdf", str(d / "source.pdf"), "--id", d.name, "--driver", "api"], log)
    for fid in targets:                                  # 指摘は使い終わったら片付ける（次の run.py で再び足さない）
        fb = fdir / f"{fid}.feedback.md"
        if fb.exists():
            fb.replace(fdir / f"{fid}.feedback.done.md")
    figs_arg = ",".join(targets)
    run([py, "scripts/judge_marks.py", d.name, "--figs", figs_arg], log)

    results, restored = [], []
    for fid in targets:
        apath, prev = fdir / f"{fid}.analysis.json", fdir / f"{fid}.analysis.prejev.json"
        after = n_flags(d, fid) if apath.exists() else None
        keep = after is not None and after < before[fid]
        if not keep:                                     # 減らなかった（か解析に失敗した）: 前の答えに戻す
            if apath.exists():
                shutil.copyfile(apath, fdir / f"{fid}.analysis.jevtry.json")
            shutil.copyfile(prev, apath)
            restored.append(fid)
        results.append({"paper": d.name, "fig": fid, "before": before[fid], "after": after,
                        "kept": keep, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if restored:                                         # 戻した図の分だけ player を組み直し、判定も戻す
        run([py, "scripts/run.py", "--pdf", str(d / "source.pdf"), "--id", d.name, "--driver", "api"], log)
        run([py, "scripts/judge_marks.py", d.name, "--figs", ",".join(restored)], log)
    with (WORK / "_fix_marks.jsonl").open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for r in results:
        print(f"  {r['paper']}/{r['fig']}: 旗 {r['before']} → {r['after']}  {'採用' if r['kept'] else '戻した'}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--figs")
    ap.add_argument("--jobs", type=int, default=1, help="並行して回す論文の数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry", action="store_true", help="一度作り直した図も、もう一度頼み直す")
    args = ap.parse_args()
    if not args.papers and not args.all:
        ap.error("論文 id か --all を指定する")
    dirs = sorted(d for d in WORK.iterdir() if (d / "judge_marks.json").exists()) if args.all \
        else [WORK / p for p in args.papers]
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        res = [r for rs in ex.map(lambda d: fix_paper(d, args), dirs) for r in rs]
    if res:
        kept = sum(r["kept"] for r in res)
        print(f"採用 {kept}/{len(res)} 図・旗 {sum(r['before'] for r in res)} → "
              f"{sum((r['after'] if r['kept'] else r['before']) for r in res)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
