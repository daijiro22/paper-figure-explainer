"""作成済みの論文の参考文献を今の読み取りで読み直し、解説の「この図で引用された文献」を更新する。API は使わない。

    python scripts/refresh_refs.py            # 全論文の差を出すだけ（何も書かない）
    python scripts/refresh_refs.py --apply [paper_id ...]

解析と声は作り直さない（run.py を流すと、画像の図の解析がやり直しになることがある）。
読み取れる文献が前より減る論文は変えない。--online で付けた題・抄録は、原文が同じ項目に引き継ぐ。
"""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import pymupdf  # noqa: E402
from pfe import cite as C  # noqa: E402

WORK = ROOT / "work"
ENRICHED = ("title", "abstract", "oa_url", "doi", "url", "year", "error")


def read_new(d: Path) -> tuple[dict, dict]:
    """(今の refs.json, 読み直した結果)。--online の結果は同じ原文の項目に引き継ぐ。"""
    old = json.loads((d / "refs.json").read_text(encoding="utf-8"))
    text = json.loads((d / "text.json").read_text(encoding="utf-8"))
    new = C.parse_references(pymupdf.open(d / "source.pdf"), text)
    by_raw = {e["raw"]: e for e in old.get("entries", [])}
    for e in new["entries"]:
        o = by_raw.get(e["raw"])
        if o:
            e.update({k: o[k] for k in ENRICHED if o.get(k)})
    return old, new


def update_bundle(d: Path, refs: dict) -> None:
    """bundle.json の各図の refs を run.py と同じ形で入れ直す（続きのページの図は親の引用）。"""
    bpath = d / "bundle.json"
    b = json.loads(bpath.read_text(encoding="utf-8"))
    figs = {f["id"]: f for f in json.loads((d / "figures.json").read_text(encoding="utf-8"))}
    entries = {e["num"]: e for e in refs["entries"]}
    for f in b.get("figures", []):
        ctx = figs.get(f["id"], {}).get("continues") or f["id"]
        f["refs"] = [{"num": r["num"], **entries.get(r["num"], {}), "context": r.get("context", "")}
                     for r in refs["by_figure"].get(ctx, [])]
    bpath.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")


def main(args: list[str]) -> int:
    apply = "--apply" in args
    names = [a for a in args if not a.startswith("--")]
    dirs = [WORK / n for n in names] if names else sorted(p.parent for p in WORK.glob("*/refs.json"))
    for d in dirs:
        if not (d / "source.pdf").exists() or not (d / "text.json").exists():
            continue
        old, new = read_new(d)
        n0, n1 = len(old.get("entries", [])), len(new["entries"])
        c0, c1 = sum(len(v) for v in old.get("by_figure", {}).values()), sum(len(v) for v in new["by_figure"].values())
        keep = n1 < n0 or (n0 > 0 and c1 < c0)      # 文献が 0 件だったのに図ごとの引用があるのは番号の誤読なので、減ってよい
        print(f"{d.name:36s} 文献 {n0:3d} → {n1:3d}  図ごと {c0:3d} → {c1:3d}  {new['style']}" + ("  減るので変えない" if keep else ""))
        if apply and not keep and (n1, c1) != (n0, c0):
            shutil.copyfile(d / "refs.json", d / "refs.json.bak_261105")
            (d / "refs.json").write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
            if (d / "bundle.json").exists():
                update_bundle(d, new)
    if apply:
        import rebuild_players
        rebuild_players.main(names)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
