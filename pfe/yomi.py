"""読み上げの読み替え: ナレーション（say）を edge-tts に渡す前に、読み間違える語を読みに置き換える。

施主 2026-11-08「読み間違いリストみたいなのを作って改善する仕組みにしたら良いか」。声は edge-tts のまま。
edge-tts は渡した文字をエスケープするので SSML の sub alias は使えない → 文字そのものを置き換える。

- 表は pfe/yomi.tsv（表記<TAB>読み<TAB>メモ）。読みが「=」の行は「そのままで正しい」と確かめた印
- 字幕（caption_html）や画面の say は変えない。置き換えるのは声に渡す文だけ（pfe/tts.py の synth）
- 何も当たらない文は 1 文字も変えずに返す（変えると音声のキャッシュの鍵が変わり、全部の声を作り直すことになる）
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

TABLE = Path(__file__).with_name("yomi.tsv")
OK = "="                        # 読みの欄がこれなら「そのままで正しく読める」

_cache: dict = {}               # (表のパス, 更新時刻) → (置き換える語, 確かめ済みの表記, (正規表現, 表記の並び) か None)


def load(path: Path | None = None) -> tuple[dict[str, str], set[str]]:
    """表を読む。(置き換える語 → 読み, 確かめ済みの表記すべて) を返す。"""
    table, _checked, _ = _compiled(path or TABLE)
    return table, _checked


def _compiled(path: Path):
    """表と、置き換え用の正規表現を返す。ファイルの更新時刻が変わったら読み直す。"""
    mtime = path.stat().st_mtime if path.exists() else 0.0
    key = (str(path), mtime)
    if key in _cache:
        return _cache[key]
    table: dict[str, str] = {}
    checked: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            surface = cols[0].strip()
            reading = cols[1].strip() if len(cols) > 1 else ""
            if not surface or not reading:
                continue
            checked.add(surface)
            if reading != OK:
                table[surface] = reading
    pat = _pattern(table)
    _cache.clear()
    _cache[key] = (table, checked, pat)
    return _cache[key]


PH = "{n}"                      # 表記の {n} は数値。読みの {n} に当たった数値を入れる（# は表のコメントの印なので使わない）
NUM = r"(\d+(?:\.\d+)?)"


def _surface_regex(s: str) -> str:
    """表記 1 つの正規表現。{n} は数値、{n} のある表記の空白は 0 個以上の空白（10 K も 10K も当たる）。"""
    if PH in s:
        body = NUM.join(re.escape(piece).replace(r"\ ", r"\s*") for piece in s.split(PH))
    else:
        body = re.escape(s)
    if s.startswith(PH):
        body = r"(?<![A-Za-z0-9.])" + body     # 1.5 K の「5」だけに当たらないよう、数値の途中からは始めない
    elif re.match(r"[A-Za-z0-9]", s[0]):
        body = r"(?<![A-Za-z0-9])" + body      # THF は THFA・2THF の中では当てない
    if s.endswith(PH) or re.match(r"[A-Za-z0-9]", s[-1]):
        body = body + r"(?![A-Za-z0-9])"
    return body


def _pattern(table: dict[str, str]) -> re.Pattern | None:
    """長い表記から順に並べた 1 本の正規表現（1 回の走査で置き換えるので、置き換えた結果は二度と置き換わらない）。
    表記ごとに名前つきの組 e0, e1, … にして、当たった表記を名前で引く。"""
    if not table:
        return None
    order = sorted(table, key=len, reverse=True)
    return re.compile("|".join(f"(?P<e{i}>{_surface_regex(s)})" for i, s in enumerate(order))), order


def _replace(m: re.Match, table: dict[str, str], order: list[str]) -> str:
    name = next(k for k, v in m.groupdict().items() if v is not None)
    s = order[int(name[1:])]
    reading = table[s]
    if PH not in s:
        return reading
    nums = re.fullmatch(_surface_regex(s), m.group(0)).groups()
    pieces = reading.split(PH)              # 読みの {n} に、当たった数値を順に入れる
    out = pieces[0]
    for i, piece in enumerate(pieces[1:]):
        out += (nums[i] if i < len(nums) else "") + piece
    return out


def to_speech(text: str, path: Path | None = None) -> str:
    """声に渡す文を作る。当たる語が無ければ text をそのまま返す。"""
    table, _, compiled = _compiled(path or TABLE)
    if compiled is None or not text:
        return text
    pat, order = compiled
    return pat.sub(lambda m: _replace(m, table, order), text)


# ---- 読み間違いの候補（英字・記号・単位の塊） ----
# 漢字の読み違いはここでは拾えない（施主が聞いて気づいたものを表に足す）
_TOKEN = re.compile(r"\([A-Za-z0-9]{1,2}\)"                                  # (a) (b) (S1) のような図の札
                    r"|[A-Za-z0-9α-ωΑ-Ω°%/+\-.~〜–·:=×′'’]+")
_NEEDS = re.compile(r"[A-Za-zα-ωΑ-Ω°%~〜×]")      # 数字だけの塊は候補にしない


def _core(t: str) -> str:
    """先頭の数値が単位につながっているときは数値を外す（353°C → °C、20mg → mg、50% → %）。
    13C・1H・5-HMF のように数値が名前の一部のもの（大文字・ハイフンが続く）はそのまま。"""
    m = re.match(r"\d+(?:\.\d+)?(?=[°%/~〜–×a-z])", t)
    t = t[m.end():] if m else t
    r = re.fullmatch(r"([〜~–])\d+(?:\.\d+)?", t)       # 1〜9・280–380 は記号だけを候補にする
    return r.group(1) if r else t


def _tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text):
        t = _core(t.strip(".,:;-"))
        if t and _NEEDS.search(t):
            out.append(t)
    return out


def _sentences(text: str) -> list[str]:
    return [s + "。" for s in text.split("。") if s.strip()]


def candidates(says: list[str], path: Path | None = None) -> list[dict]:
    """ナレーションの束から、まだ確かめていない英字・記号の塊を多い順に返す。

    数字は # にまとめて数える（353°C と 280°C は同じ「#°C」）。表で置き換えた後の文で数えるので、
    表に足した語や「=」の語はもう出てこない。example は塊を含む最も短い文（声に渡す形）。
    """
    _, checked, _ = _compiled(path or TABLE)
    count: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    example: dict[str, str] = {}
    for say in says:
        for sent in _sentences(to_speech(say, path)):
            for t in _tokens(sent):
                if t in checked:
                    continue
                k = re.sub(r"\d+(?:\.\d+)?", "#", t)
                count[k] += 1
                forms.setdefault(k, Counter())[t] += 1
                if k not in example or len(sent) < len(example[k]):
                    example[k] = sent
    return [{"key": k, "count": n, "form": forms[k].most_common(1)[0][0], "example": example[k]}
            for k, n in count.most_common()]
