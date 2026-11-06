"""テキスト側の抽出 CLI: PDF → figures.json / text.json / refs.json / meta.json（と source.pdf）。

使い方:
    py -3.13 scripts/extract_text.py --pdf <論文.pdf> --work work/<paper_id> [--online]

--online を付けたときだけ Crossref / OpenAlex に問い合わせる（cache/ にキャッシュ）。
figures_override.json があれば figures.json の代わりにそれを重ねた結果を text 抽出に使う。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from pfe import cite as C  # noqa: E402
from pfe import figures as F  # noqa: E402
from pfe import text as T  # noqa: E402
from pfe.pdfutil import all_lines, body_font_size  # noqa: E402

DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[^\s,;\"<>]+")


def build_meta(doc, pdf_path: Path) -> dict:
    """PDF のメタデータと 1 ページ目の文字から title/authors/journal/year/doi を作る。"""
    md = doc.metadata or {}
    first = all_lines(doc)[0]
    first_text = "\n".join(l["text"] for l in first)

    # DOI: メタデータ（subject 等）→ 1 ページ目の本文の順で正規表現
    doi = None
    for src in (md.get("subject") or "", md.get("keywords") or "", first_text):
        m = DOI_IN_TEXT.search(src)
        if m:
            doi = m.group(0).rstrip(".,;)")
            break

    # タイトル: メタデータ。無ければ 1 ページ目で最大フォントの行を連結
    title = (md.get("title") or "").strip()
    if not title and first:
        top = max(l["size"] for l in first)
        title = " ".join(l["text"].strip() for l in sorted(first, key=lambda l: l.y0) if l["size"] == top)

    # 著者: 表題より小さく本文より十分大きい行（Elsevier は 10.6pt）で、表題行より下にあるもの。
    # 数字・* は所属記号なので落とす。表題のサイズは「メタデータの題名で始まる行」から取る。
    authors: list[str] = []
    body = body_font_size(doc)
    tl = [l for l in first if title[:20] and l["text"].strip().startswith(title[:20])]
    if tl:
        t_size, t_y = tl[0]["size"], tl[0].y0
        cands = [l for l in first if body + 1 < l["size"] < t_size and l.y0 > t_y]
        if cands:
            a_size = max(l["size"] for l in cands)
            line = " ".join(l["text"] for l in sorted(cands, key=lambda l: l.y0) if l["size"] == a_size)
            line = re.sub(r"[\d*†‡§]+", " ", line)
            authors = [re.sub(r"\s+", " ", a).strip() for a in re.split(r",|and", line) if a.strip()]
    if not authors and md.get("author"):
        authors = [a.strip() for a in re.split(r"[;,]", md["author"]) if a.strip()]

    # 雑誌名・年: subject（"Journal of ..., 195 (2026) 107638. doi:..."）から
    subject = md.get("subject") or ""
    journal = subject.split(",")[0].strip() if "," in subject else ""
    ym = re.search(r"\((\d{4})\)", subject) or re.search(r"D:(\d{4})", md.get("creationDate") or "")
    year = int(ym.group(1)) if ym else None

    return {"title": title or pdf_path.stem, "authors": authors, "journal": journal, "year": year,
            "doi": doi, "source": str(pdf_path), "source_path_or_url": str(pdf_path)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--work", required=True, help="work/<paper_id>")
    ap.add_argument("--online", action="store_true", help="Crossref/OpenAlex で参考文献を補完")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    work = Path(args.work)
    if not work.is_absolute():
        work = ROOT / work
    work.mkdir(parents=True, exist_ok=True)
    if pdf.resolve() != (work / "source.pdf").resolve():
        shutil.copyfile(pdf, work / "source.pdf")

    def dump(name: str, obj) -> None:
        (work / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    doc = pymupdf.open(str(work / "source.pdf"))
    dump("meta.json", build_meta(doc, pdf))

    auto = F.detect_figures(doc)
    dump("figures.json", auto)
    figs = F.load_figures(work)          # figures_override.json があれば重ねる

    text = T.extract_text(doc, figs)
    dump("text.json", text)

    refs = C.parse_references(doc, text)
    if args.online:
        refs = C.enrich_online(refs, ROOT / "cache")
    dump("refs.json", refs)

    # 要約
    n_sent = sum(len(p["sentences"]) for p in text["paragraphs"])
    n_doi = sum(1 for e in refs["entries"] if e["doi"])
    print(f"figures: {len(figs)} / paragraphs: {len(text['paragraphs'])} / sentences: {n_sent}")
    print(f"図の言及: {len(text['fig_mentions'])} 種 / 参考文献: {len(refs['entries'])} 件 (DOI あり {n_doi})")
    print(f"by_figure: " + ", ".join(f"{k}={len(v)}" for k, v in refs["by_figure"].items()))
    low = [f["id"] for f in figs if f.get("confidence") == "low"]
    print("low-confidence figures: " + (", ".join(low) if low else "なし"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
