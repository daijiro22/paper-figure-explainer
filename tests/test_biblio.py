"""書誌情報の部品（pfe/biblio.py）。通信は偽物に差し替え、控えの動きまで見る。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import biblio as B  # noqa: E402

MSG = {
    "title": ["A <i>sample</i>  title"],
    "container-title": ["Journal of Examples"],
    "author": [{"family": "Doe", "given": "Jane"}, {"name": "The Example Consortium"}],
    "published-online": {"date-parts": [[2021, 3]]},
}


def test_parse_crossref_cleans_title_and_keeps_group_authors():
    m = B.parse_crossref(MSG)
    assert m["title"] == "A sample title"
    assert m["journal"] == "Journal of Examples"
    assert m["year"] == 2021
    assert m["authors"] == [{"family": "Doe", "given": "Jane"}, {"family": "The Example Consortium", "given": ""}]


def test_clean_doi():
    assert B.clean_doi("10.1073/pnas.1/-/DCSupplemental") == "10.1073/pnas.1"
    assert B.clean_doi("10.1021/a.7b00110.s001") == "10.1021/a.7b00110"
    assert B.clean_doi("10.1039/x0xx00000x") is None
    assert B.clean_doi(None) is None


def test_split_name():
    assert B.split_name("Jane Q. Doe") == {"family": "Doe", "given": "Jane Q."}
    assert B.split_name("山田 太郎") == {"family": "山田", "given": "太郎"}
    assert B.split_name("") == {"family": "", "given": ""}


class _Resp:
    """urlopen の応答の代わり。"""
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


def test_crossref_by_doi_uses_cache(tmp_path, monkeypatch):
    calls = []

    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        return _Resp({"message": MSG})

    monkeypatch.setattr(B.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    first = B.crossref_by_doi("10.1/abc", tmp_path)
    second = B.crossref_by_doi("10.1/abc", tmp_path)      # 2 回目は控えから読む
    assert first == second and first["year"] == 2021
    assert len(calls) == 1 and calls[0].endswith("/works/10.1/abc")


def test_doi_from_title_only_accepts_near_match(tmp_path, monkeypatch):
    items = {"message": {"items": [{"DOI": "10.1/x", "title": ["Something else entirely"]},
                                   {"DOI": "10.1/y", "title": ["A <i>sample</i> title"]}]}}
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda req, timeout=None: _Resp(items))
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    assert B.doi_from_title("A sample title", tmp_path) == "10.1/y"
    assert B.doi_from_title("No such paper in the results", tmp_path) is None


def test_network_failure_returns_none(tmp_path, monkeypatch):
    def boom(req, timeout=None):
        raise B.urllib.error.URLError("no network")

    monkeypatch.setattr(B.urllib.request, "urlopen", boom)
    assert B.crossref_by_doi("10.1/none", tmp_path) is None
