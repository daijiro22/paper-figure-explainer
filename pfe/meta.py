"""論文の情報（題・著者など）を、どのファイルからどの順で読むかを 1 か所に決める。

優先順: 人が直した値（meta_override.json）→ Crossref で確かめた値（biblio.json。題は除く）
→ 取り込み時に取った値（meta.json）→ bundle.json の paper。
人の修正を別ファイルにするのは、meta.json は取り込みで、bundle.json は run.py で書き直され、
SQLite は work/ から作り直すので、どこに書いても戻ってしまうため（Codex と相談・施主 2026-10-30
「たまに間違った名前で登録されるから、論文の名前を変えられるようにしたい」）。

meta_override.json に置くもの:
  title   表示する題（PDF の原稿番号「sc7b03285 1..8」などを直す）
  public  公開サイトに載せるか（無ければ載せる。scripts/export_site.py が見る）
  year・journal・authors  Crossref に無い本の論文情報。人が原文などの根拠を見て書く（authors は「名 姓」の文字列の配列）
"""
from __future__ import annotations

import json
from pathlib import Path

OVERRIDE = "meta_override.json"


def _read(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_override(d: Path) -> dict:
    return _read(d / OVERRIDE)


def write_override(d: Path, **fields) -> dict:
    """meta_override.json に fields を足して保存する（値が None の項目は消す＝元に戻す）。保存後の中身を返す。
    ファイルがあるのに読めない（同期の途中・壊れた）ときは止める（空とみなして上書きすると、題やタグが消える）。"""
    p = d / OVERRIDE
    if p.exists():
        try:
            ov = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"{p.name} が読めないので保存しない（同期の途中かもしれない）: {e}")
        ov = ov if isinstance(ov, dict) else {}
    else:
        ov = {}
    for k, v in fields.items():
        if v is None:
            ov.pop(k, None)
        else:
            ov[k] = v
    tmp = d / (OVERRIDE + ".tmp")
    tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / OVERRIDE)                   # 書きかけのファイルを残さない
    return ov


def apply_override(meta: dict, d: Path) -> dict:
    """取り込み時の meta に、人が直した値を重ねた写しを返す（元の題は title_extracted に残す）。"""
    out = dict(meta or {})
    ov = read_override(d)
    title = ov.get("title")
    if title:
        out.setdefault("title_extracted", out.get("title"))
        out["title"] = title
    for k in ("year", "journal"):
        if ov.get(k):
            out[k] = ov[k]
    if ov.get("authors"):
        out["authors"] = ov["authors"]
        out.pop("authors_family", None)     # Crossref の姓を人の修正より優先しない
    return out


def _apply_biblio(meta: dict, d: Path) -> dict:
    """Crossref で確かめた年・雑誌・著者・DOI を重ねる（scripts/fill_biblio.py が書く）。題は人の修正と PDF の値のまま。"""
    bib = _read(d / "biblio.json")
    out = dict(meta)
    for k in ("year", "journal", "doi"):
        if bib.get(k):
            out[k] = bib[k]
    if bib.get("authors"):
        out["authors"] = [" ".join(p for p in (a.get("given"), a.get("family")) if p) for a in bib["authors"]]
        out["authors_family"] = [a.get("family") or "" for a in bib["authors"]]
    return out


def paper_meta(d: Path) -> dict:
    """work/<pid>/ の論文の情報（Crossref で確かめた値と人の修正を反映済み）。"""
    base = _read(d / "meta.json") or _read(d / "bundle.json").get("paper") or {}
    return apply_override(_apply_biblio(base, d), d)


def notion_url(d: Path) -> str | None:
    """この論文の Notion のページの URL（notion/page.json。あるときだけ棚にボタンが出る）。無ければ None。"""
    url = _read(d / "notion" / "page.json").get("url") or ""
    return url if url.startswith("https://") else None


def is_public(d: Path) -> bool:
    return read_override(d).get("public", True) is not False


def thumbnail(d: Path, width: int = 480) -> Path | None:
    """一覧のカードに出す縮小画像 work/<pid>/thumb.jpg（解説のある最初の図。元の図より古ければ作り直す）。"""
    figs = [f.get("img") for f in _read(d / "bundle.json").get("figures", []) if f.get("img")]
    figs += [f"fig/{f['id']}.png" for f in (json.loads((d / "figures.json").read_text(encoding="utf-8"))
                                            if (d / "figures.json").exists() else []) if f["id"].startswith("fig")]
    src = next((d / p for p in figs if (d / p).is_file()), None)
    if src is None:
        return None
    out = d / "thumb.jpg"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width))
            im.save(out, "JPEG", quality=82)
    return out
