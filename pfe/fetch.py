"""URL から論文 PDF を取ってくる。

対応する形:
- PDF への直リンク（Content-Type が application/pdf か、中身が %PDF で始まる）
- 論文ページ（HTML）: <meta name="citation_pdf_url"> を読む（多くの出版社・PMC・bioRxiv・J-STAGE が出す）
- arXiv の abs ページ → /pdf/ に読み替え
- doi.org のリンク → 転送先のページで上と同じ処理

購読が要る論文（Elsevier など）は、ここからは取れないことが多い。そのときは手元の PDF を --pdf で渡す。
"""
from __future__ import annotations

import os
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

_CONTACT = os.environ.get("PFE_CONTACT_EMAIL", "").strip()     # 任意。出版社に連絡先を伝えたい人だけ環境変数で渡す
UA = "Mozilla/5.0 (paper-figure-explainer" + (f"; mailto:{_CONTACT}" if _CONTACT else "") + ")"


class FetchError(Exception):
    pass


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        name = (a.get("name") or a.get("property") or "").lower()
        if name and a.get("content") and name not in self.meta:
            self.meta[name] = a["content"]


def _get(url: str, timeout: int = 60) -> tuple[bytes, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", ""), r.geturl()


def id_from_url(url: str) -> str:
    """URL から作業フォルダ名を作る（arXiv 番号・DOI の末尾・ホスト名＋パスの末尾）。"""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([\d.]+)", url)
    if m:
        return "arxiv_" + m.group(1).replace(".", "_")
    m = re.search(r"10\.\d{4,9}/([^\s?#]+)", url)
    if m:
        return "doi_" + re.sub(r"\W+", "_", m.group(1))[-40:].strip("_").lower()
    u = urlparse(url)
    tail = re.sub(r"\W+", "_", (u.path.rstrip("/").split("/") or ["paper"])[-1])[:30]
    return f"{u.netloc.split('.')[-2] if '.' in u.netloc else u.netloc}_{tail}".lower()


def fetch_pdf(url: str, out: Path) -> Path:
    """url の論文 PDF を out に保存して返す。取れなければ FetchError。"""
    if out.exists() and out.stat().st_size > 0:
        return out
    m = re.search(r"arxiv\.org/abs/([\w.\-/]+)", url)
    if m:
        url = f"https://arxiv.org/pdf/{m.group(1)}"
    body, ctype, final = _get(url)
    if "pdf" in ctype.lower() or body[:5] == b"%PDF-":
        out.write_bytes(body)
        return out
    # HTML: citation_pdf_url を探す
    p = _MetaParser()
    p.feed(body.decode("utf-8", "replace"))
    pdf_url = p.meta.get("citation_pdf_url")
    if not pdf_url:
        raise FetchError(f"PDF の場所が分からない（citation_pdf_url が無い）: {final}")
    body, ctype, _ = _get(urljoin(final, pdf_url))
    if body[:5] != b"%PDF-":
        raise FetchError(f"PDF が取れなかった（購読が要るかもしれない）: {pdf_url}")
    out.write_bytes(body)
    return out
