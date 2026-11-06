"""論文の書誌情報を Crossref で確かめる部品（棚の論文情報 scripts/fill_biblio.py などが使う）。

応答は cache/ に控え、取れなければ None を返す（文献情報は PDF から取った値で続ける）。
"""
from __future__ import annotations

import difflib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "paper-figure-explainer/0.1"            # 連絡先は入れない
TITLE_MATCH = 0.9                                    # 題から DOI を探すときの、同じ論文とみなす類似度


def _get_json(url: str, cache_file: Path) -> dict | None:
    """応答を cache/ に控えて返す。取れなければ None（文献情報は PDF から取った値で続ける）。"""
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  Crossref に問い合わせできなかった: {e}")
        return None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.2)
    return data


def _plain(s: str) -> str:
    """Crossref の題に混ざるタグ（<i> など）と余分な空白を落とす。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def parse_crossref(msg: dict) -> dict:
    """Crossref の message を {title, authors:[{family, given}], journal, year} にする。"""
    authors = []
    for a in msg.get("author") or []:
        if a.get("family"):
            authors.append({"family": a["family"].strip(), "given": (a.get("given") or "").strip()})
        elif a.get("name"):
            authors.append({"family": a["name"].strip(), "given": ""})
    year = None
    for key in ("published-print", "published-online", "published", "issued"):
        parts = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            year = int(parts[0])
            break
    titles = msg.get("title") or [""]
    journals = msg.get("container-title") or [""]
    return {"title": _plain(titles[0]), "authors": authors, "journal": _plain(journals[0]), "year": year}


def clean_doi(doi: str | None) -> str | None:
    """PDF から拾った DOI を整える。補足資料の尾（PNAS の /-/DCSupplemental、ACS の .s001）を切り、雛形の DOI（RSC の x0xx00000x）は捨てる。"""
    if not doi or "x0xx" in doi:
        return None
    return re.sub(r"\.s\d{3}$", "", doi.split("/-/")[0].rstrip(".,;)"))


def crossref_by_doi(doi: str, cache: Path) -> dict | None:
    safe = re.sub(r"[^\w.-]", "_", doi)
    data = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/"), cache / f"crossref_{safe}.json")
    return parse_crossref(data["message"]) if data and data.get("message") else None


def doi_from_title(title: str, cache: Path) -> str | None:
    """題で Crossref を探す。題がほぼ同じ候補があるときだけ DOI を返す（別の論文を拾わない）。"""
    key = re.sub(r"\W+", "_", title.lower())[:80]
    url = "https://api.crossref.org/works?rows=3&select=DOI,title&query.title=" + urllib.parse.quote(title)
    data = _get_json(url, cache / f"crossref_q_{key}.json")
    for it in ((data or {}).get("message") or {}).get("items") or []:
        cand = _plain((it.get("title") or [""])[0])
        if difflib.SequenceMatcher(None, cand.lower(), title.lower()).ratio() >= TITLE_MATCH:
            return it.get("DOI")
    return None


def split_name(raw: str) -> dict:
    """PDF から取った 'First Last' を分ける。日本語名は '姓 名' のまま姓を先頭にする。"""
    parts = raw.split()
    if not parts:
        return {"family": raw, "given": ""}
    if re.search(r"[ぁ-んァ-ヶ一-龠]", raw):
        return {"family": parts[0], "given": " ".join(parts[1:])}
    return {"family": parts[-1], "given": " ".join(parts[:-1])}
