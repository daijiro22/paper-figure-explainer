"""論文をまとめて処理するときの点検。LLM を使わない。

  python scripts/bulk_check.py before <pid>...  → 解析の前。キャプションの切れ・概要の怪しさ・字間空き
  python scripts/bulk_check.py after <pid>...   → 解析のあと。図の欠け・概要・未確認・引用の取り違え
  python scripts/bulk_check.py fixcaps <pid> <図 id>... → キャプションを PDF の同じブロックの全文に直す（before の「続き」を見てから）
  python scripts/bulk_check.py sheets <pid>...  → 注記を図に重ねた一覧画像を work/_batch/sheets/ に作る（Pillow が要る）

切り出しの枠そのものは scripts/audit_crops.py で見る（ページ全体に赤枠を重ねる）。
"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WORK = Path(__file__).resolve().parent.parent / "work"
# 字間空きの判定: 英字 1 文字が空白をはさんで 3 個以上続く所
SPACED = re.compile(r"(?<![A-Za-z])[A-Za-z](?: [A-Za-z]){2,}(?![A-Za-z])")


def load_figs(pid):
    d = json.loads((WORK / pid / "figures.json").read_text(encoding="utf-8"))
    return d if isinstance(d, list) else d["figures"]


def norm(s):
    """照合用に空白・ハイフン・記号の違いをならす。"""
    s = s.replace("­", "").replace("◦", "°")
    return re.sub(r"[\s\-‐-―]+", "", s).lower()


def quotes(o):
    """analysis.json の中の quote をすべて集める。"""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "quote" and isinstance(v, str):
                yield v
            else:
                yield from quotes(v)
    elif isinstance(o, list):
        for v in o:
            yield from quotes(v)


def count_unverified(b):
    """bundle.json の unverified が真のステップを数える。"""
    n = 0
    if isinstance(b, dict):
        n += 1 if b.get("unverified") else 0
        n += sum(count_unverified(v) for v in b.values())
    elif isinstance(b, list):
        n += sum(count_unverified(v) for v in b)
    return n


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}


def missing_numbers(pid, figs):
    """本文に図番号が出るのに切り出されていない図・表を探す（Awano の "Fig. 3A–C"、Rahman の "Figure 1:" は検出から漏れた）。"""
    import pymupdf
    text = " ".join(pg.get_text() for pg in pymupdf.open(WORK / pid / "source.pdf"))
    have = {f["id"] for f in figs}
    miss = set()
    for m in re.finditer(r"\b(?:Figs?\.|FIGS?\.|Figures?|FIGURES?)\s*(\d{1,2})(?!\d)", text):   # "Figure 3f" も拾う
        if f"fig{int(m.group(1))}" not in have:
            miss.add(f"fig{int(m.group(1))}")
    for m in re.finditer(r"\b(?:Tables?|TABLES?)\s+(\d{1,2}|[IVX]{1,4})\b", text):
        n = m.group(1)
        n = int(n) if n.isdigit() else ROMAN.get(n, 0)
        if n and f"table{n}" not in have:
            miss.add(f"table{n}")
    return sorted(miss, key=lambda x: (x[0], int(re.sub(r"\D", "", x))))


def caption_rest(pid, figs):
    """キャプションの続きが PDF の同じブロックにあれば、全文を返す（1 行目で切れる抽出の検出）。"""
    import pymupdf
    doc = pymupdf.open(WORK / pid / "source.pdf")
    out = {}
    for f in figs:
        c = re.sub(r"\s+", " ", f.get("caption") or "").strip()
        if len(c) < 5:
            continue
        for b in doc[int(f["page"]) - 1].get_text("blocks"):
            t = re.sub(r"\s+", " ", b[4]).strip()
            if t.startswith(c[:min(len(c), 25)]) and len(t) > len(c) + 3:
                out[f["id"]] = t
    return out


def crop_suspects(pid, figs):
    """枠の怪しい図を PDF の中身から探す。Agarwal 2013 fig1（本文が混入）、Wang 2021 fig9（軸が切れた）、
    Geng 2018 fig2（画像の下半分が切れた）は目視でも見落とした。"""
    import pymupdf
    doc = pymupdf.open(WORK / pid / "source.pdf")
    out = []
    for f in figs:
        if not f.get("clip") or f.get("rotate"):
            continue
        page = doc[int(f["page"]) - 1]
        clip = pymupdf.Rect(f["clip"])
        words = page.get_text("words")
        if f["id"].startswith("fig"):
            # 図の枠の中に文字が多い: 本文の混入。長い語の割合も見る（図中の文字は短い）
            inside = [w for w in words if clip.contains(pymupdf.Rect(w[:4]))]
            long_words = [w for w in inside if len(w[4]) >= 6 and w[4].isalpha()]
            if len(long_words) >= 40:
                out.append(f"本文の混入 {f['id']} p{f['page']}: 枠内の長い語 {len(long_words)} 個")
            # 枠の下とキャプションの間に文字: 軸の目盛りや軸題が切れている
            cap = f.get("caption_bbox")
            if cap and cap[1] > clip.y1:
                gap = pymupdf.Rect(clip.x0, clip.y1, clip.x1, cap[1] - 1)
                in_gap = [w[4] for w in words if gap.intersects(pymupdf.Rect(w[:4])) and (w[1] + w[3]) / 2 > clip.y1 and w[3] < cap[1] + 1]
                if in_gap:
                    out.append(f"枠の下に文字 {f['id']} p{f['page']}: {' '.join(in_gap)[:60]}")
            # 画像の一部だけが枠に入っている: 枠が画像を切っている
            for im in page.get_image_info():
                r = pymupdf.Rect(im["bbox"])
                area = r.get_area()
                # 小さな飾りと、走査 PDF のページ全面の画像は対象外
                if area < 2000 or area > 0.6 * page.rect.get_area():
                    continue
                share = (r & clip).get_area() / area if r.intersects(clip) else 0
                if 0.2 < share < 0.9:
                    out.append(f"画像が切れている {f['id']} p{f['page']}: 枠に入るのは {share:.0%}")
    return out


def before(pids):
    """解析と Notion の前に直したほうが安いものを出す。出たら figures.json / abstract.json を直す。"""
    for pid in pids:
        figs = load_figs(pid)
        print(f"== {pid}: 図表 {len(figs)} 個")
        for x in crop_suspects(pid, figs):
            print(f"   {x}")
        miss = missing_numbers(pid, figs)
        if miss:
            print(f"   本文に出るのに無い: {miss}（参考文献の図番号が混ざることがあるので目で見る）")
        rest = caption_rest(pid, figs)
        for f in figs:
            c = (f.get("caption") or "").strip()
            if f["id"] in rest:
                print(f"   キャプションの続き {f['id']}: {rest[f['id']][:260]}")
            elif len(c) < 25 or re.search(r"\(See$|[-,]$|\b(and|of|at|from|the|in|on|after|before|with|for|to|by)$", c):
                print(f"   キャプションが切れている {f['id']} p{f['page']}: …{c[-80:]}")
            # ギリシャ文字と記号の崩れ（â-O-4 は β-O-4、1508C は 150 °C、Elsevier の ¼ は =）。原文で確かめて payload.json と figures.json を直す
            broken = re.findall(r"\S*[âÂãÃ¼�]\S*|\b\d{1,3} ?8C\b", c)
            if broken:
                print(f"   キャプションの文字が崩れている {f['id']}: {', '.join(broken[:5])}")
            if f.get("clip") and (f["clip"][3] <= f["clip"][1] or f["clip"][2] <= f["clip"][0]):
                print(f"   枠が崩れている {f['id']} p{f['page']}: {f['clip']}")
        ab_path = WORK / pid / "abstract.json"
        ab = json.loads(ab_path.read_text(encoding="utf-8")).get("text", "") if ab_path.exists() else ""
        if not ab:
            print("   概要: abstract.json が空（無ければ prepare のあとにできる。空なら PDF から写す）")
        elif not re.match(r"[A-Z0-9぀-鿿\"(]", ab) or not re.search(r"[.。)]\s*$", ab):
            print(f"   概要: 途中から始まるか途中で切れている: 「{ab[:50]}」…「{ab[-40:]}」")
        elif len(ab) < 300:
            print(f"   概要: 短い（{len(ab)} 字）: {ab[:80]}")
        tj = WORK / pid / "text.json"
        if tj.exists():
            n = len(SPACED.findall(tj.read_text(encoding="utf-8")))
            if n > 20:
                print(f"   本文: 字間空きが {n} 箇所 → scripts/fix_spaced_ocr.py {pid}")


def fixcaps(args):
    """fixcaps <pid> <図 id>...: PDF の同じブロックの全文でキャプションを直す（figures.json と notion/payload.json）。"""
    pid, ids = args[0], args[1:]
    rest = caption_rest(pid, load_figs(pid))
    for rel in ("figures.json", "notion/payload.json"):
        path = WORK / pid / rel
        if not path.exists():
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        for f in (d if isinstance(d, list) else d["figures"]):
            if f.get("id") in ids and f["id"] in rest:
                f["caption"] = rest[f["id"]]
                print(f"{rel} {f['id']}: {rest[f['id']][:120]}")
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def after(pids):
    """解析のあとの機械の点検。"原文に無い" のうち表以外と、他の本にある引用だけを詳しく出す。"""
    corpus = {}
    for pid in pids:
        t = json.loads((WORK / pid / "text.json").read_text(encoding="utf-8"))
        ab = json.loads((WORK / pid / "abstract.json").read_text(encoding="utf-8")).get("text", "")
        caps = " ".join(f.get("caption") or "" for f in load_figs(pid))
        corpus[pid] = norm(" ".join(p["text"] for p in t["paragraphs"]) + " " + caps + " " + ab)
    for pid in pids:
        w = WORK / pid
        ids = [f["id"] for f in load_figs(pid)]
        miss = [i for i in ids if not (w / "fig" / f"{i}.analysis.json").exists()]
        has_ov = (w / "fig" / "overview.analysis.json").exists()
        bundle = json.loads((w / "bundle.json").read_text(encoding="utf-8"))
        n_quote, odd = 0, []
        for i in ids + ["overview"]:
            p = w / "fig" / f"{i}.analysis.json"
            if not p.exists():
                continue
            for q in quotes(json.loads(p.read_text(encoding="utf-8"))):
                n_quote += 1
                if norm(q) not in corpus[pid]:
                    other = [o for o in pids if o != pid and norm(q) in corpus[o]]
                    # 表の文字は本文に無いのが普通なので、表以外か他の本にあるものだけ気にする
                    if not i.startswith("table") or other:
                        odd.append((i, q[:70], other))
        # 注記を頼んだのに何も描かれないステップ（軸の目盛りの対応が空で band_x が落ちる。検査にも [要確認] にも出ない）
        silent = []
        for i in ids:
            ap, rp = w / "fig" / f"{i}.analysis.json", w / "fig" / f"{i}.resolved.json"
            if not ap.exists() or not rp.exists():
                continue
            steps = json.loads(ap.read_text(encoding="utf-8")).get("steps", [])
            for st in json.loads(rp.read_text(encoding="utf-8")).get("steps", []):
                if st["index"] < len(steps) and steps[st["index"]].get("actions") and not st.get("shapes"):
                    silent.append(f"{i}#{st['index']}")
        size = (w / "player.html").stat().st_size / 1e6 if (w / "player.html").exists() else 0
        print(f"{pid}: 図表 {len(ids)} 欠け {miss} 概要 {has_ov} player {size:.1f}MB "
              f"未確認 {count_unverified(bundle)} 引用 {n_quote} 要確認の引用 {len(odd)} 注記の出ないステップ {silent or 0}")
        for x in odd[:8]:
            print("    ", x)


def sheets(pids):
    """注記（resolved.json）を図に重ねた一覧を作る。図 6 枚で 1 シート・色と番号はステップごと。"""
    from PIL import Image, ImageDraw
    out = WORK / "_batch" / "sheets"
    out.mkdir(parents=True, exist_ok=True)
    col = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#00a0a0", "#c000c0", "#808000", "#804000", "#000080"]
    tile_w, tile_h, ncol, per = 620, 470, 3, 6
    for pid in pids:
        ids = [f["id"] for f in load_figs(pid)]
        for n in range(0, len(ids), per):
            part = ids[n:n + per]
            rows = (len(part) + ncol - 1) // ncol
            sheet = Image.new("RGB", (tile_w * ncol, (tile_h + 18) * rows), "white")
            for i, fid in enumerate(part):
                im = Image.open(WORK / pid / "fig" / f"{fid}.png").convert("RGB")
                sc = min(tile_w / im.width, tile_h / im.height)
                im = im.resize((int(im.width * sc), int(im.height * sc)))
                d = ImageDraw.Draw(im)
                rp = WORK / pid / "fig" / f"{fid}.resolved.json"
                r = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {"steps": []}
                for st in r["steps"]:
                    c = col[st["index"] % len(col)]
                    for s in st.get("shapes", []):
                        if s.get("box"):
                            x0, y0, x1, y1 = [v * sc for v in s["box"]]
                            d.rectangle([x0, y0, x1, y1], outline=c, width=3)
                            d.text((x0 + 2, y0 + 1), str(st["index"]), fill=c)
                x, y = (i % ncol) * tile_w, (i // ncol) * (tile_h + 18)
                ImageDraw.Draw(sheet).text((x + 4, y + 2), f"{pid} {fid}  steps={len(r['steps'])}", fill="black")
                sheet.paste(im, (x, y + 18))
            name = out / f"{pid}_{n // per + 1}.png"
            sheet.save(name)
            print(name)


if __name__ == "__main__":
    fn = {"before": before, "after": after, "sheets": sheets, "fixcaps": fixcaps}.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not fn or len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    fn(sys.argv[2:])
