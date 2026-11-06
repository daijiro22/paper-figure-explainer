"""論文 PDF → 図ごとの動く解説（player.html）を一気通貫で作る。

使い方:
  py -3.13 scripts/run.py --pdf "<論文.pdf>" [--id taira2026_jaap] [--figs fig1,fig7] [--driver codex|claude|manual]
                          [--online] [--no-tts] [--force]
  py -3.13 scripts/run.py --url "<論文ページか PDF の URL>" ...

各段は冪等: 出力が既にあれば作り直さない（--force で作り直す。analysis.json は高いので --force-analysis のときだけ）。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import re
import shutil
from pathlib import Path

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pfe import figures as F, text as T, cite as C  # noqa: E402
from pfe.geometry import analyze_figure  # noqa: E402
from pfe.som import render_som  # noqa: E402
from pfe.raster import RASTER_V, add_raster_regions  # noqa: E402
from pfe.meta import apply_override  # noqa: E402
from pfe.tables import table_geometry  # noqa: E402
from pfe.analyze import analyze  # noqa: E402
from pfe.resolve import resolve_figure  # noqa: E402
from pfe.build import figure_entry, para_where, write_bundle  # noqa: E402
from pfe import fetch  # noqa: E402


def paper_id_from(path: Path) -> str:
    """ファイル名から短い英数字の id を作る（例 "Taira et al. - 2026 - ..." → taira2026_xxxx）。"""
    name = path.stem
    m = re.match(r"\s*([A-Za-z]+).*?(\d{4})", name)
    words = re.findall(r"[A-Za-z]{4,}", name)
    tail = words[3].lower() if len(words) > 3 else "paper"
    return f"{m.group(1).lower()}{m.group(2)}_{tail}" if m else re.sub(r"\W+", "_", name.lower())[:30]


def make_overview(work: Path, doc, meta: dict, figs: list[dict], bundle_figs: list[dict], args) -> dict | None:
    """概要の解説を作って bundle["overview"] の形で返す。アブストラクトが無い・まだ作れないときは None。
    台本（API を 1〜2 回）は、図と表がすべて解析済みになったときだけ作る（Web 版は図を 1 枚ずつ流すので、
    途中で作ると図が増えるたびに作り直しになる）。それ以前は、前に作った台本があれば使い回す。"""
    from pfe.abstract import extract_abstract
    from pfe.overview import analyze_overview, overview_entry
    apath = work / "abstract.json"
    if args.force or not apath.exists():
        apath.write_text(json.dumps(extract_abstract(doc) or {}, ensure_ascii=False, indent=1), encoding="utf-8")
    ab = json.loads(apath.read_text(encoding="utf-8"))
    if not ab.get("text"):
        print("■ 概要: アブストラクトが見つからないので作らない")
        return None
    ids = [b["id"] for b in bundle_figs]
    all_ids = [f["id"] for f in figs if (f["id"].startswith("fig") or f["id"].startswith("table") and f.get("clip"))]
    complete = all((work / "fig" / f"{i}.analysis.json").exists() for i in all_ids) and set(ids) == set(all_ids)
    print("■ 概要")
    if not complete and not (work / "fig" / "overview.analysis.json").exists():
        print("  図・表がまだそろっていないので、概要はあとで作る")
        return None
    a = analyze_overview(work, meta.get("title") or work.name, ab["text"], ids, args.driver if complete else "manual")
    if not a or not a.get("steps"):
        return None
    if a.get("errors"):
        print(f"  概要: 検査の指摘が残った: {a['errors'][:3]}")
    return overview_entry(work, a, ab, ids, args.no_tts)


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf")
    src.add_argument("--url")
    ap.add_argument("--id")
    ap.add_argument("--figs", help="対象の図 id をカンマ区切り（省略で全部）")
    ap.add_argument("--driver", default="codex", choices=["codex", "api", "claude", "manual"])
    ap.add_argument("--online", action="store_true", help="Crossref/OpenAlex で引用文献の題・抄録を補う")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--no-overview", action="store_true", help="概要（アブストラクトの解説）を作らない")
    ap.add_argument("--force", action="store_true", help="幾何・画像・bundle を作り直す")
    ap.add_argument("--force-analysis", action="store_true", help="LLM の解析も作り直す")
    args = ap.parse_args()

    # ---- 0. 入力 ----
    if args.url:
        pid = args.id or fetch.id_from_url(args.url)
        work = ROOT / "work" / pid
        work.mkdir(parents=True, exist_ok=True)
        pdf_path = fetch.fetch_pdf(args.url, work / "source.pdf")
    else:
        pdf_path = Path(args.pdf)
        pid = args.id or paper_id_from(pdf_path)
        work = ROOT / "work" / pid
        work.mkdir(parents=True, exist_ok=True)
        if not (work / "source.pdf").exists():
            shutil.copyfile(pdf_path, work / "source.pdf")
    print(f"作業フォルダ: {work}")
    doc = pymupdf.open(work / "source.pdf")
    figdir = work / "fig"
    figdir.mkdir(exist_ok=True)

    # ---- 1. 本文・図の位置・引用 ----
    if args.force or not (work / "figures.json").exists():
        figs = F.detect_figures(doc)
        (work / "figures.json").write_text(json.dumps(figs, ensure_ascii=False, indent=1), encoding="utf-8")
    figs = F.load_figures(work)
    if args.force or not (work / "text.json").exists():
        text = T.extract_text(doc, figs)
        (work / "text.json").write_text(json.dumps(text, ensure_ascii=False, indent=1), encoding="utf-8")
    text = json.loads((work / "text.json").read_text(encoding="utf-8"))
    if args.force or not (work / "refs.json").exists():
        refs = C.parse_references(doc, text)
        (work / "refs.json").write_text(json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")
    refs = json.loads((work / "refs.json").read_text(encoding="utf-8"))
    if args.online:
        refs = C.enrich_online(refs, ROOT / "cache")
        (work / "refs.json").write_text(json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = json.loads((work / "meta.json").read_text(encoding="utf-8")) if (work / "meta.json").exists() else {
        "title": doc.metadata.get("title") or pdf_path.stem, "authors": [doc.metadata.get("author", "")],
        "journal": doc.metadata.get("subject", ""), "year": None, "doi": None, "source": str(pdf_path)}
    meta = apply_override(meta, work)            # 人が直した題（meta_override.json）を player にも出す

    paras = {p["id"]: p for p in text["paragraphs"]}
    para_ids = list(paras)
    where = para_where(text)
    entries_by_num = {e["num"]: e for e in refs.get("entries", [])}
    # 図と表（施主 2026-10-30「Table とかも取れたら良い」）。表は図のあとに並ぶ（figures.json の順）
    targets = [f for f in figs if (f["id"].startswith("fig") or f["id"].startswith("table") and f.get("clip"))]
    if args.figs:
        want = set(args.figs.split(","))
        targets = [f for f in targets if f["id"] in want]

    bundle_figs = []
    for fig in targets:
        fid = fig["id"]
        print(f"■ {fid}（p{fig['page']}・{fig.get('confidence', '?')}）")
        png = figdir / f"{fid}.png"
        gpath = figdir / f"{fid}.geom.json"
        som = figdir / f"{fid}.som.png"
        page, clip = F.figure_view(doc, fig)     # 向きの違う図表は正立させたページと、その上の枠
        rotate = fig.get("rotate") or 0
        # ---- 2. 切り出しと幾何・番号札 ----
        # 向きが変わった図は、画像と幾何があっても作り直す（幾何が変わるので解析も署名の違いでやり直しになる）
        stale = gpath.exists() and (json.loads(gpath.read_text(encoding="utf-8")).get("rotate") or 0) != rotate
        if args.force or stale or not png.exists() or not gpath.exists():
            scale = F.render_figure(doc, fig, png, long_edge=1800)
            if fig.get("kind") == "table":
                # 表は軸の検出に通さない（罫線を軸の枠と取り違える）。文字のある表はセル・行を候補にし、
                # 文字の無い（画像の）表は画像の図と同じく画素から切り出す（下の raster の段）
                geom = table_geometry(page, clip) or {"clip": list(clip), "kind": "raster", "panels": [], "regions": []}
            else:
                geom = analyze_figure(page, clip)
            geom["scale"] = scale
            if rotate:
                geom["rotate"] = rotate
            gpath.write_text(json.dumps(geom, ensure_ascii=False), encoding="utf-8")
            render_som(str(png), geom, scale, str(som))
        geom = json.loads(gpath.read_text(encoding="utf-8"))
        scale = geom["scale"]
        if geom.get("kind") == "raster" and geom.get("raster_v") != RASTER_V:
            # 画像の図: 画素から部品の候補を切り出して G 番号を足す（前に作った幾何にも足す。番号が変わるので解析はやり直し）
            n = add_raster_regions(geom, str(png))
            gpath.write_text(json.dumps(geom, ensure_ascii=False), encoding="utf-8")
            render_som(str(png), geom, scale, str(som))
            print(f"  画像から部品の候補を {n} 個切り出した")

        # ---- 3. LLM の解析 ----
        mentions = []
        ctx_id = fig.get("continues") or fid     # 続きのページの図は親の図の言及・引用を文脈にする（本文は親の番号で書く）
        for m in text.get("fig_mentions", {}).get(ctx_id, []):
            p = paras.get(m["para"])
            if p and not any(x["para"] == p["id"] for x in mentions):
                mentions.append({"para": p["id"], "text": p["text"]})
                # 段落がページの切れ目で文の途中のまま終わっているときは、続きの段落も根拠に入れる
                # （Hololignin Fig.5: 図の読みどころが続きの段落にだけ書かれていた）
                nxt = para_ids.index(p["id"]) + 1
                if not re.search(r"[.!?。)\]\d]\s*$", p["text"]) and nxt < len(para_ids):
                    q = paras[para_ids[nxt]]
                    if not any(x["para"] == q["id"] for x in mentions):
                        mentions.append({"para": q["id"], "text": q["text"]})
        sources = {"caption": fig["caption"], **{f"para:{m['para']}": m["text"] for m in mentions}}
        if geom.get("table_text"):
            sources["table"] = geom["table_text"]      # 表の中の文字も根拠（引用と数値の照合）に使う
        fig_refs = []
        for r in refs.get("by_figure", {}).get(ctx_id, []):
            e = entries_by_num.get(r["num"], {})
            # 参考文献の一覧が読めなかった論文でも番号は残す（Springer で一覧が 0 件のとき KeyError 'num' で落ちた）
            fig_refs.append({"num": r["num"], **e, "context": r.get("context", "")})
        apath = figdir / f"{fid}.analysis.json"
        if args.force_analysis:
            # 生の答えも捨てる（残すと下の「回収」で前回の答えがそのまま使われる）
            apath.unlink(missing_ok=True)
            (figdir / f"{fid}.analysis.raw.json").unlink(missing_ok=True)
        if apath.exists():
            from pfe.analyze import normalize, geom_signature
            analysis = normalize(json.loads(apath.read_text(encoding="utf-8")))
            sig = analysis.get("geom_sig")
            if sig is None and args.driver == "manual":
                # manual で手書きした答え（署名なし）は、今の番号札を見て書いたものとして署名を付ける。検査は 1 回かける
                from pfe.analyze import check_analysis
                errs = check_analysis(json.loads(apath.read_text(encoding="utf-8")), geom, sources)
                print(f"  手書きの答えを受け取った（検査の指摘 {len(errs)} 件）")
                for e in errs[:8]:
                    print(f"    - {e}")
                analysis["geom_sig"] = geom_signature(geom)
                apath.write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
            elif sig != geom_signature(geom):  # 署名が無い古い答えも、番号の対応を確かめられないのでやり直す
                # 幾何を作り直して番号が変わった: 古い答えは別の曲線を指しうるので捨ててやり直す
                print("  幾何の番号が変わったので解析をやり直す")
                apath.replace(apath.with_suffix(".stale.json"))  # 前の .stale があっても上書き
        raw = figdir / f"{fid}.analysis.raw.json"
        raw_sig = figdir / f"{fid}.analysis.raw.sig"
        from pfe.analyze import geom_signature as _gs
        if (not apath.exists() and raw.exists() and raw_sig.exists()
                and raw_sig.read_text(encoding="utf-8").strip() == _gs(geom)):
            # 前回、答えは返ったのに時間切れ扱いで捨てられた分を回収する（検査はやり直す）
            from pfe.analyze import check_analysis, normalize, geom_signature
            try:
                a = json.loads(raw.read_text(encoding="utf-8"))
                errs = check_analysis(a, geom, sources)
                print(f"  前回の答えを回収（検査の指摘 {len(errs)} 件）")
                for e in errs[:5]:
                    print(f"    - {e}")
                a = normalize(a)
                a["geom_sig"] = geom_signature(geom)
                apath.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  前回の答えは使えない: {e}")
        if apath.exists():
            from pfe.analyze import normalize
            analysis = normalize(json.loads(apath.read_text(encoding="utf-8")))
        else:
            res = analyze(fig, geom, sources, mentions, fig_refs, som, png, figdir, driver=args.driver)
            if res["status"] != "ok":
                print(f"  解析できず（{res['status']}）: {res['errors'][:3]}")
                continue
            if res["errors"]:
                print(f"  検査の指摘が残った: {res['errors'][:5]}")
            analysis = res["analysis"]

        # ---- 4. 画素の図形へ ----
        resolved = resolve_figure(geom, analysis, scale)
        (figdir / f"{fid}.resolved.json").write_text(json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")
        for s in resolved["steps"]:
            for w in s["warnings"]:
                print(f"  警告 step{s['index']}: {w}")

        # ---- 5. 音声 ----
        audio = []
        if not args.no_tts:
            from pfe.tts import synth
            for st in analysis.get("steps", []):
                try:
                    p, d = synth(st["say"], work / "audio")
                    audio.append((f"audio/{p.name}", d))
                except Exception as e:  # 音声が作れなくても再生ページは作る（Web Speech に退避）
                    print(f"  音声を作れなかった: {e}")
                    audio.append((None, None))

        with Image.open(png) as im:
            size = im.size
        bundle_figs.append(figure_entry(fig, f"fig/{fid}.png", size, analysis, resolved, sources, where, fig_refs, audio))

    # ---- 6. 概要（アブストラクトの解説。再生の最初に 1 回だけ流す） ----
    overview = None if args.no_overview else make_overview(work, doc, meta, figs, bundle_figs, args)

    # ---- 7. 組み立て ----
    write_bundle(work / "bundle.json", meta, bundle_figs, overview)
    sys.path.insert(0, str(ROOT / "player"))
    from build_player import build
    build(str(work / "bundle.json"), str(work / "player.html"))
    print(f"できあがり: {work / 'player.html'}（図 {len(bundle_figs)} 枚）")
    try:
        from pfe.syncscan import request_scan
    except ImportError:     # 2 台の PC の同期のための部品。公開版には入っていない
        return 0
    request_scan()      # もう一台の PC へ届ける（検知を取りこぼすことがあるため）
    return 0


if __name__ == "__main__":
    sys.exit(main())
