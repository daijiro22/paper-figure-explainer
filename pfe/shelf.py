"""棚のカードに出す論文情報（年・雑誌の略称・著者）を決める。手元の棚と研究室サイトで同じものを使う。

施主 2026-11-06「検索がしづらい。登録日より論文の年を、ジャーナルの省略形を、著者名ももう少しわかりやすく」。
元の値は pfe.meta.paper_meta（Crossref で確かめた biblio.json を優先）。雑誌の略称は下の表で引き、
表に無い雑誌は正式名のまま出す（略称を推測で作らない）。組み立ての出力に、表に無い雑誌を出す。
"""
from __future__ import annotations

import re

from pfe.biblio import split_name

MAX_ALL_AUTHORS = 4     # これ以下なら全員の姓を出す。多いときは先頭 3 人と最後の人（責任著者のことが多い）

# 正規化した雑誌名 → 略称。出版社が Crossref に登録した略称を優先し（2026-11-06 に突き合わせ）、登録が無い雑誌は
# その分野で通用する形（ISO 4・CASSI）。根拠が定まらない雑誌は載せず正式名のまま出す。PNAS だけは短い通称を使う
JOURNAL_ABBR = {
    "acs sustainable chemistry and engineering": "ACS Sustainable Chem. Eng.",
    "biomacromolecules": "Biomacromolecules",
    "biomass conversion and biorefinery": "Biomass Conv. Bioref.",
    "bioresource technology": "Bioresour. Technol.",
    "bioresources": "BioResources",
    "biotechnology and bioengineering": "Biotechnol. Bioeng.",
    "biotechnology for biofuels": "Biotechnol. Biofuels",
    "canadian journal of chemistry": "Can. J. Chem.",
    "cellulose": "Cellulose",
    "chemsuschem": "ChemSusChem",
    "coatings": "Coatings",
    "composites part a applied science and manufacturing": "Compos. Part A Appl. Sci. Manuf.",
    "energy advances": "Energy Adv.",
    "energy and fuels": "Energy Fuels",
    "enzyme and microbial technology": "Enzyme Microb. Technol.",
    "frontiers in bioengineering and biotechnology": "Front. Bioeng. Biotechnol.",
    "frontiers in chemistry": "Front. Chem.",
    "green chemistry": "Green Chem.",
    "holzforschung": "Holzforschung",
    "industrial crops and products": "Ind. Crops Prod.",
    "international journal of molecular sciences": "Int. J. Mol. Sci.",
    "japan tappi journal": "Japan TAPPI Journal",       # Crossref の登録は J. Tappi J. で、定まった略称が確かめられない
    "japanese journal of applied physics": "Jpn. J. Appl. Phys.",
    "journal of agricultural and food chemistry": "J. Agric. Food Chem.",
    "journal of analytical and applied pyrolysis": "J. Anal. Appl. Pyrolysis",
    "journal of chemical engineering and industrial biotechnology": "JCEIB",     # 論文の見出しにある雑誌自身の略称
    "journal of cleaner production": "J. Clean. Prod.",
    "journal of the american chemical society": "J. Am. Chem. Soc.",
    "journal of wood chemistry and technology": "J. Wood Chem. Technol.",
    "journal of wood science": "J. Wood Sci.",
    "paper trade journal": "Pap. Trade J.",
    "physical chemistry chemical physics": "Phys. Chem. Chem. Phys.",
    "plant physiology": "Plant Physiol.",
    "plant physiology and biochemistry": "Plant Physiol. Biochem.",
    "polymers": "Polymers",
    "process biochemistry": "Process Biochem.",
    "proceedings of the 14th international symposium on wood fibre and pulping chemistry": "Proc. 14th ISWFPC",
    "proceedings of the national academy of sciences": "PNAS",
    "proceedings of the national academy of sciences of the united states of america": "PNAS",
    "protoplasma": "Protoplasma",
    "rsc advances": "RSC Adv.",
    "rsc sustainability": "RSC Sustain.",
    "small structures": "Small Struct.",
    "the plant journal": "Plant J.",
    "wood science and technology": "Wood Sci. Technol.",
}
# Crossref の雑誌名が出版社の内部コードになっているもの → 正式名
JOURNAL_FIX = {"hfsg": "Holzforschung"}


def clean_journal(raw: str | None) -> str:
    """PDF から拾った雑誌名の後ろに付いた DOI・年・巻号を落とす。"""
    s = re.split(r",\s*(?:https?://|doi:)", raw or "", maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\s*\(\d{4}\).*$", "", s)           # RSC Advances (2024)、RSC Sustainability (2023), 1, 1192-1199
    s = re.sub(r"\s+\d+\s*\(\d+\).*$", "", s)       # Paper Trade Journal 122(2), 35-43
    s = s.strip(" ,")
    return JOURNAL_FIX.get(s.lower(), s)


def _journal_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).strip()


def journal_short(raw: str | None) -> str:
    """雑誌の略称。表に無ければ（余計な文字を落とした）正式名。"""
    name = clean_journal(raw)
    return JOURNAL_ABBR.get(_journal_key(name), name)


def known_journal(raw: str | None) -> bool:
    return _journal_key(clean_journal(raw)) in JOURNAL_ABBR


def family_names(meta: dict) -> list[str]:
    """著者の姓の並び。Crossref の姓を優先し、無ければ PDF の名前の最後の語（日本語名は最初の語）。"""
    if meta.get("authors_family"):
        return [n for n in meta["authors_family"] if n]
    return [split_name(a.strip())["family"] for a in meta.get("authors") or [] if a and a.strip()]


def author_line(families: list[str]) -> str:
    """カードに出す著者。例: 'Agarwal, Reiner, Ralph'、'Meng, Pu, Sannigrahi … Ragauskas'。"""
    if len(families) <= MAX_ALL_AUTHORS:
        return ", ".join(families)
    return ", ".join(families[:3]) + " … " + families[-1]


def shelf_info(meta: dict) -> dict:
    """カードに出す値と、検索に足す文字列（正式名・略称・点を抜いた略称・姓）を返す。"""
    families = family_names(meta)
    journal, short = clean_journal(meta.get("journal")), journal_short(meta.get("journal"))
    words = [journal, short, short.replace(".", ""), *families]
    return {"year": meta.get("year") or "", "journal": journal, "journal_short": short,
            "authors_line": author_line(families), "first_author": families[0] if families else "",
            "search": " ".join(w for w in words if w)}
