"""図・表の切り出しが正しく取れているかを Jev に判定させ、work/<pid>/judge.json に書く（figures.json は触らない）。

    python scripts/judge_figs.py hololignin2018 taira2026_jaap   # 論文を指定
    python scripts/judge_figs.py --all                            # work/*/ を全部
    python scripts/judge_figs.py jia2017_acs --figs fig1,fig2     # 図を絞る
    python scripts/judge_figs.py jia2017_acs --dry-run            # 送る画像と依頼だけ作る（API を呼ばない）
    python scripts/judge_figs.py --all --eval                     # 判定済みの結果を既知の悪い例と突き合わせる

送った画像と依頼は work/<pid>/judge/ に残る（赤枠の位置を目で確かめる用）。
同じ clip・同じ質問なら判定をやり直さない（--force でやり直す）。質問としきい値は pfe/judge.py の先頭。
Windows で pymupdf が動かない PC は WSL の venv で回す。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import judge  # noqa: E402
from pfe.figures import load_figures  # noqa: E402

WORK = ROOT / "work"

# 切り出しが悪いと目で確かめた図。判定がこれを拾えるかでしきい値を合わせる。
# 値は「立つべき旗」。ここに無い図は正解が分からない（良いとは限らない）。
# [要確認] Hololignin Fig.3/5/6・Jia Fig.1/2/4/5 は「上端が 10pt 削れたまま」との記録があるが、
# 2026-10-31 に closeup を見た限り赤枠の上は余白で、欠けは目で分からなかった。確かめてから足す。
# None は「目で見て問題なし」（旗が立てば誤検出）。正解はその clip に付けたものなので、clip が変わった図は突き合わせない。
KNOWN = {
    # 直す前の Sapouna（2026-10-31 に fig6/8/9 を直した。控え figures.json.bak_261031）
    ("sapouna2021_lignin", "fig9", (38.4, 237.5, 300.6, 395.1)): "stray_text",   # 上端に Fig.8 のキャプション 2 行目
    ("sapouna2021_lignin", "fig6", (66.9, 47.2, 300.6, 242.0)): "missing_part",  # 右のパネル (b) を切り落とし（Jev の初回で発見）
    ("sapouna2021_lignin", "fig8", (47.4, 47.2, 300.6, 230.1)): "missing_part",  # 右のパネル (b)(c) を切り落とし（同上）
    # 直したあとの Sapouna（closeup で確認）
    ("sapouna2021_lignin", "fig6", (67.0, 47.2, 528.8, 236.2)): None,
    ("sapouna2021_lignin", "fig8", (47.5, 47.2, 545.3, 223.8)): None,
    ("sapouna2021_lignin", "fig9", (38.4, 247.5, 300.6, 395.1)): None,
    ("hololignin2018", "fig6", (320.2, 68.8, 601.5, 308.7)): "stray_text",  # 縦書き「Downloaded from pubs.acs.org」（crop で確認）
    ("taira2026_jaap", "fig7", (337.6, 311.4, 525.9, 556.8)): "stray_text",  # 縦書き「… by KYOTO UNIV user」（同上）
    ("taira2026_jaap", "fig2", (38.9, 387.7, 287.2, 720.8)): None,
    ("taira2026_jaap", "fig8", (68.9, 52.0, 257.2, 250.4)): None,
    ("taira2026_jaap", "fig11", (38.9, 543.2, 287.2, 709.2)): None,
}


def papers(args) -> list[Path]:
    if args.all:
        return sorted(d for d in WORK.iterdir() if (d / "figures.json").exists() and (d / "source.pdf").exists())
    return [WORK / p for p in args.papers]


def judge_paper(d: Path, args, key: str | None) -> dict:
    """1 本の論文の図を判定し、judge.json（{"figures": {id: 結果}}）を書いて返す。"""
    import pymupdf
    out_path = d / "judge.json"
    res = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"figures": {}}
    doc = pymupdf.open(d / "source.pdf")
    want = set(args.figs.split(",")) if args.figs else None
    for fig in load_figures(d):
        if want and fig["id"] not in want:
            continue
        if not fig.get("clip") or not fig.get("page"):
            continue
        old = res["figures"].get(fig["id"])
        if old and old.get("sig") == judge.signature(fig) and not args.force and not args.dry_run:
            continue
        save = d / "judge"
        if args.dry_run:
            imgs = judge.make_images(doc, fig)
            save.mkdir(exist_ok=True)
            for k, im in imgs.items():
                im.save(save / f"{fig['id']}.{k}.png")
            body = judge.build_request(fig, judge.new_refs())
            (save / f"{fig['id']}.request.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {d.name}/{fig['id']}: 画像 {imgs['page'].size} {imgs['closeup'].size} → {save}")
            continue
        try:
            res["figures"][fig["id"]] = judge.judge_figure(doc, fig, key, save)
        except judge.JudgeError as e:
            print(f"  {d.name}/{fig['id']}: 失敗 {e}")
            if e.kind in ("no_key", "auth"):
                raise
            continue
        out_path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")   # 1 図ごとに残す
    return res


def show(d: Path, res: dict) -> None:
    """判定を 1 図 1 行で出す。"""
    for fid, r in res.get("figures", {}).items():
        flags = "・".join(judge.FLAG_JA.get(f, f) for f in r["flags"]) or "-"
        p = " ".join(f"{k}={v:.2f}" for k, v in r["p"].items())
        iou = "" if r.get("box_iou") is None else f" IoU={r['box_iou']:.2f}"
        print(f"  {r['verdict']:5s} {d.name}/{fid}: {flags}  ({p}{iou})")


def evaluate(dirs: list[Path]) -> None:
    """既知の悪い例を拾えたか（再現率）と、それ以外で旗が立った割合を出す。しきい値を決める材料。"""
    hit, miss, good_ok, false_flag, other_flag, other_n = [], [], 0, [], 0, 0
    for d in dirs:
        p = d / "judge.json"
        if not p.exists():
            continue
        figs = {f["id"]: f for f in load_figures(d)}
        for fid, r in json.loads(p.read_text(encoding="utf-8")).get("figures", {}).items():
            if fid not in figs or r.get("sig") != judge.signature(figs[fid]):
                continue        # 判定のあとで clip が変わった（まだ判定し直していない）
            key = (d.name, fid, tuple(figs[fid].get("clip") or ()))
            if key not in KNOWN:
                other_n += 1
                other_flag += bool(r["flags"])
            elif KNOWN[key] is None:
                if r["flags"]:
                    false_flag.append(f"{d.name}/{fid}({','.join(r['flags'])})")
                else:
                    good_ok += 1
            else:
                want = KNOWN[key]
                (hit if want in r["flags"] else miss).append(f"{d.name}/{fid}({want} p={r['p'].get(want)})")
    print(f"既知の悪い例: {len(hit)}/{len(hit) + len(miss)} 拾えた")
    for m in miss:
        print(f"  拾えず: {m}")
    print(f"既知の良い例: {good_ok}/{good_ok + len(false_flag)} 旗なし")
    for m in false_flag:
        print(f"  誤検出: {m}")
    print(f"それ以外: {other_flag}/{other_n} 図で旗（正解不明。目で見て誤検出かを確かめる）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("papers", nargs="*", help="work/ の下の論文 id")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--figs", help="fig1,table2 のように絞る")
    ap.add_argument("--force", action="store_true", help="判定済みでもやり直す")
    ap.add_argument("--dry-run", action="store_true", help="画像と依頼を work/<pid>/judge/ に書くだけ")
    ap.add_argument("--eval", action="store_true", help="判定済みの結果を既知の悪い例と突き合わせるだけ")
    args = ap.parse_args()
    if not args.papers and not args.all:
        ap.error("論文 id か --all を指定する")
    dirs = papers(args)
    if args.eval:
        evaluate(dirs)
        return 0
    key = None if args.dry_run else judge.api_key()
    for d in dirs:
        print(f"[{d.name}]")
        res = judge_paper(d, args, key)
        if not args.dry_run:
            show(d, res)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except judge.JudgeError as e:
        print(f"止めた: {e}", file=sys.stderr)
        sys.exit(2)
