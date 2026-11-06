"""解説の各ステップの注記が意味のある所を指しているかを Jev に判定させ、work/<pid>/judge_marks.json に書く。

    python scripts/judge_marks.py taira2026_jaap sapouna2021_lignin   # 論文を指定
    python scripts/judge_marks.py --all
    python scripts/judge_marks.py taira2026_jaap --figs fig1,fig2 --dry-run   # 見せる画像だけ作る
    python scripts/judge_marks.py --all --sheet          # 旗の立ったステップを一覧の画像にまとめる（目で確かめる用）

見せた画像は work/<pid>/judge_marks/<図>_s<ステップ>.png に残る。同じ注記・同じナレーションなら判定し直さない。
質問としきい値は pfe/judge_marks.py の先頭。bundle.json は書き換えない。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import judge, judge_marks as JM  # noqa: E402

WORK = ROOT / "work"


def papers(args) -> list[Path]:
    if args.all:
        return sorted(d for d in WORK.iterdir() if (d / "bundle.json").exists())
    return [WORK / p for p in args.papers]


def run_paper(d: Path, args, key) -> dict:
    """1 本の論文の全ステップを判定し（判定済みは飛ばす）、judge_marks.json を書いて返す。"""
    bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
    out_path = d / "judge_marks.json"
    res = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"steps": {}}
    want = set(args.figs.split(",")) if args.figs else None
    # 解析を作り直してステップが減った図の、もう無いステップの判定は消す（旗を数え間違えないように）
    valid = {f"{f['id']}#{i}" for f in bundle.get("figures", []) for i, s in enumerate(f.get("steps", []))
             if JM.marks_of(s)}
    res["steps"] = {k: v for k, v in res["steps"].items() if k in valid}
    jobs = []
    for fig in bundle.get("figures", []):
        if want and fig["id"] not in want:
            continue
        for si, step in enumerate(fig.get("steps", [])):
            if not JM.marks_of(step):
                continue
            sid = f"{fig['id']}#{si}"
            old = res["steps"].get(sid)
            if old and old.get("sig") == JM.signature(fig, step) and not args.force and not args.dry_run:
                continue
            jobs.append((sid, fig, si, step))

    def one(job):
        sid, fig, si, step = job
        try:
            return sid, JM.judge_step(d, fig, step, key, d / "judge_marks" / f"{fig['id']}_s{si}.png")
        except judge.JudgeError as e:
            if e.kind in ("no_key", "auth"):
                raise
            return sid, {"verdict": "unchecked", "error": str(e), "flags": []}   # 失敗は「未検査」（合格にしない）
        except Exception as e:     # 描けない注記など。1 ステップのために全体を止めない
            return sid, {"verdict": "unchecked", "error": f"{type(e).__name__}: {e}", "flags": []}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for sid, r in ex.map(one, jobs):
            if r is not None and not args.dry_run:
                res["steps"][sid] = r
    res["steps"] = {k: JM.reflag(v) for k, v in res["steps"].items()}    # 今のしきい値で旗を立て直す
    if not args.dry_run:
        out_path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{d.name}] 判定 {len(jobs)} ステップ")
    return res


def show(d: Path, res: dict) -> None:
    bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
    figs = {f["id"]: f for f in bundle.get("figures", [])}
    rs = res.get("steps", {})
    n_flag = sum(1 for r in rs.values() if r.get("flags"))
    n_un = sum(1 for r in rs.values() if r.get("verdict") == "unchecked")
    print(f"  旗 {n_flag}/{len(rs)} ステップ（未検査 {n_un}）")
    for sid, r in rs.items():
        if not r.get("flags"):
            continue
        fid, si = sid.split("#")
        step = figs.get(fid, {}).get("steps", [])[int(si)] if fid in figs else {}
        marks = JM.marks_of(step)
        detail = []
        for f in r["flags"]:
            m, kind = f.split(":")
            i = int(m[1:]) - 1
            lab = (marks[i].get("label") or "") if i < len(marks) else ""
            pe, pr = r["p"].get(f"{m}_empty"), r["p"].get(f"{m}_relevant")
            detail.append(f"{m}({marks[i]['kind'] if i < len(marks) else '?'}{'「' + lab + '」' if lab else ''})"
                          f"={JM.FLAG_JA[kind]} empty={pe} rel={pr}")
        print(f"  {d.name}/{fid} s{si}: {JM._plain(step.get('caption_html'))[:40]} | " + " / ".join(detail))


JA_FONTS = ("/mnt/c/Windows/Fonts/meiryo.ttc", r"C:\Windows\Fonts\meiryo.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


def _ja_font(size: int):
    """一覧の字幕・ナレーション用の日本語フォント（既定のフォントだと豆腐になる）。"""
    from PIL import ImageFont
    for p in JA_FONTS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return JM._font(size)


def _wrap(text: str, n: int) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def sheet(dirs: list[Path], out: Path) -> None:
    """旗の立ったステップの画像を、ナレーションの字幕と一緒に縦に並べる（目で確かめる用）。"""
    from PIL import Image, ImageDraw
    rows = []
    for d in dirs:
        p = d / "judge_marks.json"
        if not p.exists():
            continue
        bundle = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
        figs = {f["id"]: f for f in bundle.get("figures", [])}
        for sid, r in json.loads(p.read_text(encoding="utf-8")).get("steps", {}).items():
            r = JM.reflag(r)
            if not r.get("flags"):
                continue
            fid, si = sid.split("#")
            png = d / "judge_marks" / f"{fid}_s{si}.png"
            if png.exists():
                st = figs[fid]["steps"][int(si)]
                labels = " ".join(f"M{i}「{m.get('label')}」" for i, m in enumerate(JM.marks_of(st), 1) if m.get("label"))
                cap = st.get("say", "") + (f"  札: {labels}" if labels else "")
                rows.append((r.get("severity", 0), f"{d.name}/{fid} s{si}  {', '.join(r['flags'])}  ({r.get('severity')})",
                             cap, png))
    if not rows:
        print("旗なし")
        return
    rows = [x[1:] for x in sorted(rows, key=lambda x: -x[0])]      # ひどい順
    W = 900
    f = _ja_font(20)
    tiles = []
    for head, cap, png in rows:
        im = Image.open(png).convert("RGB")
        im.thumbnail((W, 640))
        lines = _wrap(cap, 42)            # ナレーション全文（と札）を 42 字で折り返す
        top = 34 + 26 * len(lines) + 6
        t = Image.new("RGB", (W, im.height + top), "white")
        dr = ImageDraw.Draw(t)
        dr.text((6, 4), head, fill="black", font=f)
        for j, ln in enumerate(lines):
            dr.text((6, 34 + 26 * j), ln, fill=(0, 90, 200), font=f)
        t.paste(im, (0, top))
        tiles.append(t)
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in out.parent.glob(f"{out.stem}_*.png"):     # 前回の一覧（同じ名前の生成物）を消してから書く
        old.unlink()
    for k in range(0, len(tiles), 8):        # 8 枚ずつ 1 ファイル
        part = tiles[k:k + 8]
        s = Image.new("RGB", (W, sum(t.height + 8 for t in part)), (80, 80, 80))
        y = 0
        for t in part:
            s.paste(t, (0, y))
            y += t.height + 8
        s.save(out.with_name(f"{out.stem}_{k // 8 + 1}.png"))
    print(f"{len(rows)} ステップを {out.parent} に書いた")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--figs")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sheet", action="store_true", help="判定済みの旗を画像にまとめるだけ")
    args = ap.parse_args()
    if not args.papers and not args.all:
        ap.error("論文 id か --all を指定する")
    dirs = papers(args)
    if args.sheet:
        sheet(dirs, WORK / "_judge_marks_sheet" / "flags.png")
        return 0
    key = None if args.dry_run else judge.api_key()
    for d in dirs:
        res = run_paper(d, args, key)
        if not args.dry_run:
            show(d, res)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except judge.JudgeError as e:
        print(f"止めた: {e}", file=sys.stderr)
        sys.exit(2)
