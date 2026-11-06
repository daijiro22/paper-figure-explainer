"""棚に出す論文情報を Crossref で確かめて、work/<pid>/biblio.json に保存する。

    py -3.13 scripts/fill_biblio.py              # まだ biblio.json が無い本だけ
    py -3.13 scripts/fill_biblio.py <pid>...     # 指定した本を取り直す
    py -3.13 scripts/fill_biblio.py --all        # 全部を取り直す
    py -3.13 scripts/fill_biblio.py --dry-run    # 書かずに結果だけ見る
    py -3.13 scripts/fill_biblio.py <pid> --trust-doi    # 題が違っても保存する（人が原文で同じ論文だと確かめたとき。和文の英題など）

PDF から拾った年・雑誌・著者には抜けと誤りがある（Lawoko の年が 1910 など）。Crossref の題が、こちらの題と
ほぼ同じときだけ保存する（別の論文の情報を取り込まない）。確かめられなかった本は書かずに一覧へ出す。
Crossref に無い本は meta_override.json に人が根拠を見て year・journal・authors を書く（pfe/meta.py）。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import difflib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import biblio as N  # noqa: E402
from pfe.meta import paper_meta  # noqa: E402
from pfe.shelf import known_journal  # noqa: E402

WORK = ROOT / "work"
CACHE = WORK / "_crossref"      # Crossref の応答の控え（棚の本の一覧には入らない）


def similar(a: str, b: str) -> float:
    """題の近さ。大文字小文字と記号の差は見ない。"""
    norm = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()     # noqa: E731
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def on_shelf(d: Path) -> bool:
    """解説が 1 つ以上ある本（棚に並ぶ本）か。"""
    if not (d / "player.html").is_file() or not (d / "bundle.json").is_file():
        return False
    return bool(json.loads((d / "bundle.json").read_text(encoding="utf-8")).get("figures"))


def lookup(d: Path, trust_doi: bool = False) -> tuple[dict | None, str]:
    """(biblio.json に書く中身, 確かめられなかった理由) を返す。"""
    meta = paper_meta(d)
    title = meta.get("title") or ""
    doi = N.clean_doi(meta.get("doi"))
    how = "DOI"
    if not doi:
        doi = N.clean_doi(N.doi_from_title(title, CACHE)) if title else None     # 補足資料の DOI を拾ったら本体へ
        how = "題"
    if not doi:
        return None, "DOI が無く、題でも見つからない"
    cr = N.crossref_by_doi(doi, CACHE)
    if not cr:
        return None, f"Crossref に無い（{doi}）"
    ratio = similar(cr["title"], title)
    if ratio < N.TITLE_MATCH and not trust_doi:
        return None, f"題が違う（{ratio:.2f}）こちら「{title[:50]}」 Crossref「{cr['title'][:50]}」"
    return {"source": "crossref", "found_by": how, "doi": doi, "title": cr["title"], "authors": cr["authors"],
            "journal": cr["journal"], "year": cr["year"], "title_match": round(ratio, 3)}, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="棚の論文情報を Crossref で確かめる")
    ap.add_argument("pids", nargs="*")
    ap.add_argument("--all", action="store_true", help="biblio.json がある本も取り直す")
    ap.add_argument("--dry-run", action="store_true", help="書かずに結果だけ見る")
    ap.add_argument("--trust-doi", action="store_true", help="題が違っても保存する（人が原文で確かめたときだけ）")
    args = ap.parse_args(argv)
    if args.trust_doi and not args.pids:
        ap.error("--trust-doi は本を指定して使う")
    dirs = [WORK / p for p in args.pids] if args.pids else [d for d in sorted(WORK.iterdir()) if d.is_dir() and on_shelf(d)]
    if not args.pids and not args.all:
        dirs = [d for d in dirs if not (d / "biblio.json").exists()]
    done, failed, changed = 0, [], []
    for d in dirs:
        before = paper_meta(d)
        bib, why = lookup(d, args.trust_doi)
        if not bib:
            failed.append(f"{d.name}: {why}")
            continue
        done += 1
        if str(before.get("year") or "") != str(bib.get("year") or ""):
            changed.append(f"{d.name}: 年 {before.get('year') or '（無し）'} → {bib.get('year')}")
        if not args.dry_run:
            (d / "biblio.json").write_text(json.dumps(bib, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"対象 {len(dirs)} 本: 確かめた {done} 本・できなかった {len(failed)} 本")
    if changed:
        print("\n年が変わる本:")
        print("\n".join("  " + c for c in changed))
    if failed:
        print("\n確かめられなかった本（書いていない）:")
        print("\n".join("  " + f for f in failed))
    unknown = sorted({m["journal"] for m in (paper_meta(d) for d in dirs) if m.get("journal") and not known_journal(m["journal"])})
    if unknown:
        print("\n略称の表（pfe/shelf.py）に無い雑誌:")
        print("\n".join("  " + j for j in unknown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
