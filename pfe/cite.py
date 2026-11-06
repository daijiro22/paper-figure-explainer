"""参考文献リストの解析と図ごとの引用の対応づけ（refs.json を作る）、オンライン補完。

- parse_references(doc, text): 「References」見出し以降の行を読み順に並べ、番号（[1] / 1. / (1) / 1) / 1 A.）で項目に分ける。
  見出しが無ければ（PNAS・JJAP）番号の並びを後ろから探す。番号が無ければ（著者年の方式）ぶら下げ字下げで分ける。
  DOI は行折り返しで途切れる（"https://doi.org/10.1016/j. biotechadv..."）ので空白を詰めて復元する。
- by_figure: 契約どおり「図に言及した文」と「その直後の 1 文」に現れた引用だけを入れる（段落全体は入れない）。
  引用の書き方は [n]（Elsevier）・上付き（ACS・RSC・JJAP）・(n)（PNAS）・(著者, 年)（著者年の方式）。
- enrich_online: --online のときだけ呼ぶ。Crossref / OpenAlex で題・年・抄録・OA リンクを埋める。
  結果は cache_dir に JSON で保存し、失敗は例外にせず refs の項目に error として記録する。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from .pdfutil import Line, all_lines, column_of, in_margin

_CONTACT = os.environ.get("PFE_CONTACT_EMAIL", "").strip()     # 任意。Crossref・OpenAlex に連絡先を伝えたい人だけ環境変数で渡す
USER_AGENT = "paper-figure-explainer/0.1" + (f" (mailto:{_CONTACT})" if _CONTACT else "")
REQUEST_INTERVAL = 0.2     # 秒。API への礼儀として連続リクエストの間を空ける
TIMEOUT = 15               # 秒

# 参考文献の項目の書き出し: "[1]"（Elsevier）・"1."（Springer は "\t 1.\t Lieth H"・PNAS）・"(1)"（ACS）・
# "1 Z. Sun"（RSC）・"1)"（JJAP）
_ENTRY_START = re.compile(r"^\s*(?:\[(\d+)\]|\((\d+)\)|(\d+)\.(?=\s)|(\d+)\)(?=\s)|(\d+)(?=\s+[A-Z]))\s*")
# ACS の見出しは "■ REFERENCES"
_REF_HEAD = re.compile(r"^[\s■▪●•]*(?:references|notes and references|references and notes|literature cited)\s*$", re.I)


def _entry_num(m) -> int:
    """_ENTRY_START の一致から項目番号を取り出す（どの書き方の組が当たったか問わない）。"""
    return int(next(g for g in m.groups() if g))
_DOI_RE = re.compile(r"(?:https?://\s*doi\.\s*org/\s*|doi:\s*)(10\.\d{4,9}/\s*\S.*)$", re.I)
_DOI_BARE = re.compile(r"(10\.\d{4,9}/\s*\S.*)$")


def _without_running(doc, by_page) -> list[list[Line]]:
    """ページの上下 12% にあって 2 ページ以上で同じ文字列（数字は同一視）の行を除く。余白の帯より内側に来る脚が
    最後の項目に混ざり、論文自身の DOI を文献の DOI と読んだ（ACS の "10878 Article DOI: 10.1021/jacs.6b03285"）。"""
    def edge(l, pno):
        return l.y1 < doc[pno].rect.height * 0.12 or l.y0 > doc[pno].rect.height * 0.88

    pages_of: dict[str, set[int]] = {}
    for pno, lines in enumerate(by_page):
        for l in lines:
            if edge(l, pno):
                pages_of.setdefault(re.sub(r"\d+", "#", l["text"].strip()), set()).add(pno)
    return [[l for l in lines if not (edge(l, pno) and len(pages_of[re.sub(r"\d+", "#", l["text"].strip())]) >= 2)]
            for pno, lines in enumerate(by_page)]


def _reference_lines(doc) -> list[Line]:
    """References 見出し以降の行を読み順（ページ順、左段→右段）に返す。"""
    by_page = _without_running(doc, all_lines(doc))
    start = None
    for pno, lines in enumerate(by_page):
        for l in lines:
            # 見出しは太字の "References"。RSC は太字でなく "References"・"Notes and references" もある
            if _REF_HEAD.match(l["text"]) and (l["bold"] or l["text"].strip() != l["text"].strip().lower()):
                start = (pno, l)
        if start:
            break
    if not start:
        return _numbered_tail(doc, by_page)
    spno, head = start
    out: list[Line] = []
    for pno in range(spno, len(by_page)):
        pw = doc[pno].rect.width
        ph = doc[pno].rect.height
        cols = {"left": [], "right": []}
        for l in by_page[pno]:
            if in_margin(l, ph):
                continue
            col = column_of(l.x0, l.x1, pw)
            col = "left" if col == "full" else col
            if pno == spno:
                # 見出しと同じ段では見出しより下だけ。見出しより前の段（左段なら上の節）は除く
                hc = "left" if column_of(head.x0, head.x1, pw) == "full" else column_of(head.x0, head.x1, pw)
                # 見出しの枠は縦に大きいことがあり（ACS "■REFERENCES" は高さ 32pt）、下端で切ると項目 (1) を落とす
                if col == hc and l.y0 <= (head.y0 + head.y1) / 2:
                    continue
                if hc == "right" and col == "left":
                    continue
            cols[col].append(l)
        for c in ("left", "right"):
            out.extend(sorted(cols[c], key=lambda l: l.y0))
    return out


def _numbered_tail(doc, by_page) -> list[Line]:
    """見出しの無い参考文献（PNAS・JJAP）: 後ろのページから「1 で始まり、番号が 5 件以上増えていく行の並び」を探す。"""
    def reading(pno):
        pw, ph = doc[pno].rect.width, doc[pno].rect.height
        cols = {"left": [], "right": []}
        for l in by_page[pno]:
            if not in_margin(l, ph):
                c = column_of(l.x0, l.x1, pw)
                cols["left" if c == "full" else c].append(l)
        return sorted(cols["left"], key=lambda l: l.y0) + sorted(cols["right"], key=lambda l: l.y0)
    pages = [reading(p) for p in range(len(by_page))]
    for pno in range(len(pages) - 1, len(pages) // 2 - 1, -1):
        for i, l in enumerate(pages[pno]):
            m = _ENTRY_START.match(l["text"])
            if not m or _entry_num(m) != 1:
                continue
            tail = pages[pno][i:] + [x for p in pages[pno + 1:] for x in p]
            last = 1
            for t in tail[1:]:
                mt = _ENTRY_START.match(t["text"])
                if mt and last < _entry_num(mt) <= last + 3:
                    last = _entry_num(mt)
            if last >= 5:
                return tail
    return []


# ---- 番号の無い一覧（著者年の方式。Elsevier の多く・Springer の一部・Wiley）--------------------------

_AY_STOP = re.compile(r"^\s*(?:publisher|supplementary|supporting information|appendix)", re.I)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2}[a-z]?)\b")
_AY_CITE = re.compile(r"((?:[A-Z][a-z]+\s)?[A-Z][\w'’\-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][\w'’\-]+)?,?\s*\(?"
                      r"((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)")


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _author_year_raws(doc, lines) -> list[str]:
    """ぶら下げ字下げで項目に分ける（項目の 1 行目が段の左端、続きの行は字下げ）。字下げが無ければ分けない。"""
    from collections import Counter
    if not lines:
        return []
    size = Counter(round(l["size"], 1) for l in lines).most_common(1)[0][0]
    groups: dict[tuple, list] = {}
    for l in lines:
        if abs(l["size"] - size) > 0.6:
            continue                         # 同じページの謝辞などの別の大きさの字は入れない
        col = column_of(l.x0, l.x1, doc[l["page"] - 1].rect.width)
        groups.setdefault((l["page"], "left" if col == "full" else col), []).append(l)
    raws: list[str] = []
    n_lines = n_indented = 0
    stop = False
    for key in sorted(groups, key=lambda k: (k[0], k[1] != "left")):
        left = min(l.x0 for l in groups[key])
        for l in groups[key]:
            if _AY_STOP.match(l["text"]):
                stop = True
                break
            n_lines += 1
            if l.x0 <= left + 2.5 or not raws:
                raws.append(l["text"].strip())
            else:
                n_indented += 1
                prev = raws[-1]
                raws[-1] = (prev[:-1] + l["text"].strip()) if prev.endswith("­") else prev + " " + l["text"].strip()
        if stop:
            break
    if n_lines == 0 or n_indented / n_lines < 0.2:
        return []
    return [re.sub(r"\s+", " ", r).strip() for r in raws if _YEAR_RE.search(r) and len(r) >= 30 and r[:1].isalpha()]


def _author_year_cites(sentence: str, entries: list[dict]) -> list[int]:
    """"(Capanema et al., 2001; Joshi et al., 2005)" "Zhang et al. (2010)" を一覧の番号に結び付ける。一覧に無いものは捨てる。"""
    nums: list[int] = []
    for m in _AY_CITE.finditer(sentence):
        for year in re.split(r"\s*,\s*", m.group(2)):
            for name in (m.group(1), m.group(1).split()[-1]):
                key = _letters(name)
                # 同じ著者・同じ年の 2023a/2023b は文字まで見る。見つからなければ年の数字だけで探す
                hits = [e for e in entries if e["_key"].startswith(key) and year in e["raw"]] or \
                       [e for e in entries if e["_key"].startswith(key) and year[:4] in e["raw"]]
                if hits:
                    nums.append(hits[0]["num"])
                    break
    return nums


# ---- 番号の引用の書き方（"[n]" は text.find_cites）--------------------------------------------------

_SUP_TOKEN = re.compile(r"^[\d,\s–−\-)]*\d[\d,\s–−\-)]*$")
_SUP_IN_TEXT = re.compile(r"(?<=[A-Za-z.,;:)\]’”])(\d{1,3}(?:\s?[,–−\-]\s?\d{1,3})*)\)?(?=[\s.,;:)]|$)")
_PAREN_CITE = re.compile(r"\((\d{1,3}(?:\s*[,–−\-]\s*\d{1,3})*)\)")


def _expand(body: str) -> list[int]:
    """"59,60" "12−15" を番号の並びに展開する。"""
    nums: list[int] = []
    for part in re.split(r"\s*,\s*", body):
        r = re.fullmatch(r"(\d+)\s*[–−\-]\s*(\d+)", part.strip())
        if r and int(r.group(1)) <= int(r.group(2)) < int(r.group(1)) + 100:
            nums.extend(range(int(r.group(1)), int(r.group(2)) + 1))
        elif part.strip().isdigit():
            nums.append(int(part.strip()))
    return nums


def _superscripts(doc) -> set[str]:
    """本文の上付きの数字（ACS・RSC・JJAP の引用番号）を、空白と ")" を除いた文字列で集める。"""
    out: set[str] = set()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                big = max((s["size"] for s in ln["spans"]), default=0)
                for s in ln["spans"]:
                    t = s["text"].strip()
                    if t and _SUP_TOKEN.match(t) and (s["flags"] & 1 or s["size"] < 0.85 * big):
                        out.add(re.sub(r"[\s)]", "", t).strip(","))
    return out


def _superscript_cites(sentence: str, tokens: set[str], n: int) -> list[int]:
    """文の中で語や句読点に直接続く数字のうち、上付きとして PDF に現れるものだけを引用とみなす。
    "m2" "CO2" は上付き・下付きの数字と文字列が同じになるので、前の語が 2 字以下か大文字で終わるものは外す。"""
    nums: list[int] = []
    for m in _SUP_IN_TEXT.finditer(sentence):
        if re.sub(r"\s", "", m.group(1)) not in tokens:
            continue
        if re.search(r"\d[.,]$", sentence[:m.start()]):
            continue                         # 小数と桁区切り（"1.5 ps" の 5 を引用と読まない）
        word = re.search(r"([A-Za-z]+)$", sentence[:m.start()])
        if word and (len(word.group(1)) < 3 or word.group(1)[-1].isupper()):
            continue
        nums.extend(x for x in _expand(m.group(1)) if 1 <= x <= n)
    return nums


def _paren_cites(sentence: str, n: int) -> list[int]:
    """"(22)" "(24, 25)"（PNAS）。"""
    return [x for m in _PAREN_CITE.finditer(sentence) for x in _expand(m.group(1)) if 1 <= x <= n]


def _clean_doi(raw: str) -> str | None:
    """項目の原文から DOI を取り出し、空白を詰めて末尾の句読点を落とす。"""
    m = _DOI_RE.search(raw) or _DOI_BARE.search(raw)
    if not m:
        return None
    doi = re.sub(r"\s+", "", m.group(1)).rstrip(".,;)〉>")
    doi = doi.replace("­", "")
    return doi or None


def _first_author(raw: str) -> str | None:
    """"[1] J.E. White, W.J. ..." → "White"（最初の著者の姓。イニシャルの後ろの語）。"""
    body = _ENTRY_START.sub("", raw)
    first = body.split(",")[0].strip()
    m = re.findall(r"[A-Z][A-Za-z'’\-]+", first)
    # "J.E. White" のイニシャルは 1〜2 文字なので、3 文字以上の最後の語を姓とみなす
    names = [w for w in m if len(w) > 2 or not w.isupper()]
    return (names[-1] if names else (m[-1] if m else None))


def _year_of(raw: str) -> int | None:
    m = re.search(r"\((\d{4})\)", raw) or re.search(r"\b((?:19|20)\d{2})[a-z]?\b", raw)   # ACS は "2007, 126, 014101"
    return int(m.group(1)) if m else None


def parse_references(doc, text: dict) -> dict:
    """refs.json の dict を返す。text は extract_text の結果。"""
    lines = _reference_lines(doc)
    raws: list[tuple[int, str]] = []
    cur_num, cur_parts = None, []
    for l in lines:
        m = _ENTRY_START.match(l["text"])
        # 番号は 1 から順に増える。前の番号の 1〜3 先だけを新しい項目とみなす（"1 A. Author" 型では
        # 続きの行の "102 Smith" なども書き出しに見えるため。1 件読み落としても次で追いつける幅）
        num = _entry_num(m) if m else None
        if num is not None and (num == 1 if cur_num is None else cur_num < num <= cur_num + 3):
            if cur_num is not None:
                raws.append((cur_num, cur_parts))
            cur_num, cur_parts = num, [l["text"].strip()]
        elif cur_num is not None:
            cur_parts.append(l["text"].strip())
    if cur_num is not None:
        raws.append((cur_num, cur_parts))

    entries = []
    for num, parts in raws:
        raw = ""
        for p in parts:                       # 行末ソフトハイフンは詰め、それ以外は空白で連結
            raw = (raw[:-1] + p) if raw.endswith("­") else ((raw + " " + p) if raw else p)
        raw = re.sub(r"\s+", " ", raw).strip()
        doi = _clean_doi(raw)
        entries.append({
            "num": num, "raw": raw, "doi": doi, "title": None, "year": _year_of(raw),
            "first_author": _first_author(raw),
            "url": f"https://doi.org/{doi}" if doi else None,
            "abstract": None, "oa_url": None,
        })
    style = "numeric"
    if not entries:                       # 番号の無い一覧（著者年の方式）。num は一覧での順番
        for num, raw in enumerate(_author_year_raws(doc, lines), 1):
            doi = _clean_doi(raw)
            year = _YEAR_RE.search(raw).group(1)
            first = re.split(r"[,\s]", raw, maxsplit=1)[0]
            entries.append({
                "num": num, "raw": raw, "doi": doi, "title": None, "year": _year_of(raw), "first_author": first,
                "url": f"https://doi.org/{doi}" if doi else None, "abstract": None, "oa_url": None,
                "label": f"{first} {year}", "_key": _letters(raw[:40]),
            })
        style = "author_year" if entries else style

    # 文の中の引用の書き方を決める: "[n]" があればそれだけ（前からの動き）。無ければ上付き → "(n)" の順
    n_entries = len(entries)
    sentences = [s for p in text["paragraphs"] for s in p["sentences"]]
    if style == "author_year":
        cites_of = lambda s: _author_year_cites(s["text"], entries)
    elif not entries or any(s["cites"] for s in sentences):
        cites_of = lambda s: s["cites"]
    else:
        tokens = _superscripts(doc)
        # 多く見つかる方を採る（"10−5" などの上付きが少し混ざるので、あるか無いかでは決めない）
        n_sup = sum(len(_superscript_cites(s["text"], tokens, n_entries)) for s in sentences)
        n_paren = sum(len(_paren_cites(s["text"], n_entries)) for s in sentences)
        if n_sup >= n_paren:
            cites_of = lambda s: _superscript_cites(s["text"], tokens, n_entries)
        else:
            cites_of = lambda s: _paren_cites(s["text"], n_entries)

    # 図ごとの引用: 図に言及した文とその直後の 1 文だけ（段落全体は付けない）
    by_figure: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for p in text["paragraphs"]:
        sents = p["sentences"]
        for i, s in enumerate(sents):
            for fid in s["figs"]:
                for j in (i, i + 1):
                    if j >= len(sents):
                        continue
                    for n in cites_of(sents[j]):
                        key = (fid, n, p["id"], j)
                        if key in seen:
                            continue
                        seen.add(key)
                        by_figure.setdefault(fid, []).append(
                            {"num": n, "para": p["id"], "sentence": j, "context": sents[j]["text"]})
    for e in entries:
        e.pop("_key", None)
    return {"style": style, "entries": entries, "by_figure": by_figure}


# ---------------------------------------------------------------------------
# オンライン補完（--online のときだけ）
# ---------------------------------------------------------------------------

_last_request = [0.0]


def _get_json(url: str, cache_file: Path) -> tuple[dict | None, str | None]:
    """キャッシュがあればそれを返す。無ければ取得して保存。失敗は (None, エラー文)。"""
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8")), None
        except Exception:
            pass                               # 壊れたキャッシュは取り直す
    wait = REQUEST_INTERVAL - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _last_request[0] = time.time()
        if e.code == 404:                      # 未登録は正常な「無い」として記録
            cache_file.write_text(json.dumps({"_not_found": True}), encoding="utf-8")
            return {"_not_found": True}, None
        return None, f"HTTP {e.code}"
    except Exception as e:                     # タイムアウト・DNS・JSON 不正など
        _last_request[0] = time.time()
        return None, f"{type(e).__name__}: {e}"
    _last_request[0] = time.time()
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data, None


def _abstract_from_inverted(inv: dict | None) -> str | None:
    """OpenAlex の abstract_inverted_index（語→位置リスト）を文章に戻す。"""
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos)) or None


def enrich_online(refs: dict, cache_dir) -> dict:
    """DOI のある項目に title/year/abstract/oa_url を埋める。refs をその場で更新して返す。
    失敗は例外にせず項目の "error" に残す。"""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    for e in refs["entries"]:
        doi = e.get("doi")
        if not doi:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doi)
        errors = []
        cr, err = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/"),
                            cache / f"crossref_{safe}.json")
        if err:
            errors.append("crossref: " + err)
        elif cr and not cr.get("_not_found"):
            msg = cr.get("message", {})
            if msg.get("title"):
                e["title"] = msg["title"][0]
            for k in ("issued", "published-print", "published-online"):
                dp = (msg.get(k) or {}).get("date-parts")
                if dp and dp[0] and dp[0][0]:
                    e["year"] = int(dp[0][0])
                    break
        oa, err = _get_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/"),
                            cache / f"openalex_{safe}.json")
        if err:
            errors.append("openalex: " + err)
        elif oa and not oa.get("_not_found"):
            if not e.get("title") and oa.get("title"):
                e["title"] = oa["title"]
            e["abstract"] = _abstract_from_inverted(oa.get("abstract_inverted_index"))
            loc = oa.get("best_oa_location") or {}
            e["oa_url"] = loc.get("pdf_url") or loc.get("landing_page_url") or (oa.get("open_access") or {}).get("oa_url")
        if errors:
            e["error"] = "; ".join(errors)
    return refs
