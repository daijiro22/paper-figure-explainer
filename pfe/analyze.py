"""図 1 枚を LLM に読ませて analysis.json を作る段。

- 依頼文（prompt.md）と JSON Schema を作り、交換可能な「呼び出し口（driver）」に渡す。
    codex  : `codex exec -i <画像> --output-schema <schema>`（既定。画像を添付できる）
    api    : Claude API（`ANTHROPIC_API_KEY`・画像を添付・structured outputs。Web 版の既定）
    claude : `claude -p -` ＋ `--json-schema`（画像は絶対パスで Read させる）
    manual : 依頼文と画像だけ書き出して止まる（人や Claude Code の対話セッションが analysis.json を書く）
- 返ってきた JSON をスキーマ＋意味の検査（番号の実在・軸の所属・引用の実在）にかけ、
  壊れていればエラー箇所を添えて 1 回だけ再依頼する。それでも駄目なら status=failed で保存。
- 失敗の種類（タイムアウト・権限・上限・JSON 不正）を分けて記録する（Codex 指摘）。

LLM には座標を言わせない。番号（T/C/L/K/P）と、データ値（°C など）と、図全体 0〜1000 の正規化座標（候補が
無い図の退避路）だけを答えさせる。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

ACTION_TYPES = [
    "highlight_x_axis", "highlight_y_axis", "spotlight_curve", "circle_peak", "band_x", "band_y",
    "box_label", "zoom_label", "zoom_panel", "box_norm", "arrow_norm",
]

_num_or_null = {"type": ["number", "null"]}
_str_or_null = {"type": ["string", "null"]}
_pair_or_null = {"type": ["array", "null"], "items": {"type": "number"}}

# OpenAI の strict 構造化出力に合わせ、全プロパティを required にして null を許す形で書く
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fig_type", "summary_ja", "panels", "steps", "takeaways_ja"],
    "properties": {
        "fig_type": {"type": "string", "enum": ["line_plot", "bar", "chromatogram", "spectrum", "scheme",
                                                  "structure", "photo", "table_like", "multi", "other"]},
        "summary_ja": {"type": "string"},
        "takeaways_ja": {"type": "array", "items": {"type": "string"}},
        "panels": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "axes", "curves", "unsure"],
            "properties": {
                "id": {"type": "string"},
                "axes": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["axis", "title", "unit", "meaning_ja", "ticks"],
                    "properties": {
                        "axis": {"type": "string", "enum": ["x", "x_top", "y_left", "y_right"]},
                        "title": {"type": "string"}, "unit": _str_or_null, "meaning_ja": {"type": "string"},
                        "ticks": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False, "required": ["id", "value"],
                            "properties": {"id": {"type": "string"}, "value": _num_or_null}}},
                    }}},
                "curves": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "name", "axis"],
                    "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                                   "axis": {"type": "string", "enum": ["y_left", "y_right"]}}}},
                "unsure": {"type": "array", "items": {"type": "string"}},
            }}},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["say", "caption_html", "point", "kind", "actions", "evidence"],
            "properties": {
                "say": {"type": "string"}, "caption_html": {"type": "string"}, "point": _str_or_null,
                "kind": {"type": "string", "enum": ["fact", "background"]},
                "actions": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["type", "panel", "side", "labels", "curve", "peak", "x_range", "y_range",
                                 "axis", "extremum", "label", "box", "from", "to"],
                    "properties": {
                        "type": {"type": "string", "enum": ACTION_TYPES},
                        "panel": _str_or_null, "side": _str_or_null,
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "curve": _str_or_null, "peak": _str_or_null,
                        "x_range": _pair_or_null, "y_range": _pair_or_null,
                        "axis": _str_or_null, "extremum": _str_or_null, "label": _str_or_null,
                        "box": _pair_or_null, "from": _pair_or_null, "to": _pair_or_null,
                    }}},
                "evidence": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["source", "quote"],
                    "properties": {"source": {"type": "string"}, "quote": {"type": "string"}}}},
            }}},
    },
}


# ---- 依頼文 ---------------------------------------------------------------

def describe_candidates(geom: dict) -> str:
    """幾何解析の候補を文章にする（画像の番号札と同じ番号）。"""
    lines = []
    if geom.get("kind") == "table":
        ids = ", ".join(g["id"] for g in geom.get("regions", []))
        unit = geom.get("table_unit", "セル")
        return (f"（これは表。軸・曲線は無いので panels は [] でよい。fig_type は table_like）\n"
                f"- 表の{unit}（緑の札。PDF の文字の位置から取ったので正確）: {ids}。"
                "注目する行・列・数字は box_label の labels に G 番号を入れて示す（1 行を囲むならその行の G をまとめて入れる）。"
                "表の一部に寄るなら zoom_label。box_norm は使わない")
    if not geom.get("panels"):
        lines.append("（この図には軸つきのグラフ枠が見つからなかった。図式・構造式・写真などとして扱う）")
        if geom.get("regions"):
            ids = ", ".join(g["id"] for g in geom["regions"])
            lines.append(f"- 図の部品のまとまり（緑の札）: {ids}。注目箇所は box_label / zoom_label の labels に G 番号を入れて示す"
                         "（複数の G を入れると、近いものはまとめて 1 つの枠、離れたものは別々の枠になる）。部品どうしの流れは arrow_norm でもよい")
        else:
            lines.append("- 部品の候補も無い。注目箇所は box_norm / arrow_norm の正規化座標 0〜1000 で示す")
        if geom.get("kind") == "raster":
            lines.append("- この図は画像なので、緑の札は画素から切り出した候補（文字の語・色の塊・小さな図形）。"
                         "**囲む所は、できるだけ G 番号で選ぶ**（座標の目測はずれやすい）。"
                         "合う G が無いときだけ box_norm を使い、番号札つき画像の縁の青い目盛り（0〜1000）と薄い格子で位置を読む")
    for p in geom.get("panels", []):
        lines.append(f"- パネル {p['id']}（灰色の枠）")
        by_side: dict[str, list[str]] = {}
        for t in p["ticks"]:
            if t["id"]:
                by_side.setdefault(t["side"], []).append(t["id"])
        names = {"left": "左の縦軸", "right": "右の縦軸", "bottom": "下の横軸", "top": "上の横軸"}
        for side, ids in by_side.items():
            lines.append(f"  - {names[side]}の目盛り数字（青の札）: {', '.join(ids)}")
        for c in p["curves"]:
            pk = ", ".join(f"{k['id']}（{'ピーク' if k['kind'] == 'peak' else '谷'}）" for k in c.get("peaks", []))
            lines.append(f"  - 曲線 {c['id']}: 色 {c['color']}" + (f"・候補 {pk}" if pk else ""))
        for l in p["labels"]:
            lines.append(f"  - 図中の文字の塊 {l['id']}（桃色の札）: 色 {l['color']}" + ("・縦書き" if l["rotated"] else ""))
    return "\n".join(lines)


def build_prompt(fig: dict, geom: dict, mentions: list[dict], refs_for_fig: list[dict],
                 som_path: Path, clean_path: Path, inline_images: bool) -> str:
    """LLM への依頼文。人格指定（「あなたは〇〇システム」）は claude -p が拒否するので書かない。"""
    ment = "\n".join(f"- [para:{m['para']}] {m['text']}" for m in mentions) or "（本文での言及は見つからなかった）"
    refs = "\n".join(f"- [{r['num']}] {r.get('raw', '')[:200]}" for r in refs_for_fig) or "（なし）"
    img_note = ("添付の 2 枚の画像を見てください（1 枚目＝番号札つき、2 枚目＝元の図）。" if inline_images else
                f"次の 2 枚の画像を Read で開いて見てください。\n1. 番号札つき: {som_path}\n2. 元の図: {clean_path}")
    # 表: 表の中の文字を原文として渡す（数値の根拠にできる）。流れも図用（軸 → 線）から表用に替える
    table = ""
    if geom.get("table_text"):
        table = ("\n## 表の中の文字（原文。source は \"table\"。1 行＝表の 1 行、セルの間は空白 1 つ、上から順）\n"
                 + geom["table_text"] + "\n\n表の解説の流れは「表の全体 → 行と列の見出し（何が並んでいるか）→ 読みどころ（大きい・小さい・"
                 "変化・比べどころ）→ まとめ」。表の数値は、この表の文字を evidence（source \"table\"・数値を含む行の"
                 "連続した一部を 20 字以上）にすればナレーションに使ってよい。\n")
    return f"""論文の図 1 枚を、大学 1〜2 年生にもわかる日本語の「動く解説」にするための下書きを JSON で作ってください。

{img_note}

## この図のキャプション（原文）
{fig['caption']}
{table}
## 本文でこの図に触れている文（原文。[para:…] が出典の番号）
{ment}

## この図の説明の中で本文が引用している文献
{refs}

## 画像に描き込んだ番号札（機械で見つけた候補）
{describe_candidates(geom)}

## 答え方
1. panels: 各パネルについて、軸ごとに題（画像の文字のとおり）・単位・意味（日本語で、その分野を知らない人向けに 1 文）を書き、
   目盛り数字の札（T 番号）それぞれに、画像に書かれている数値を value で答える（読めなければ null）。
   軸の数字なのに T 番号が付かず L 番号（桃色）になっているものは、その L 番号を ticks に入れて値を答えてよい。
   曲線（C 番号）それぞれに、凡例や図中の文字から分かる名前（例「スギ木材の DTG」）と、どちらの縦軸に属するかを答える。
   迷ったことは unsure に書く。
2. steps: 再生の 1 コマずつ（4〜8 コマ）。流れは「図の全体 → 横軸 → 縦軸 → 何の線か → 読みどころ（ピーク・範囲・差）→ まとめ」。
   - say: ナレーション 1〜3 文。です・ます調。1 文 40 字前後。
   - caption_html: 字幕（say を短くしたもの）。大事な語を <em>…</em> で 1〜2 個囲む。
   - point: このコマが図の一番大事な主張なら 12〜20 字の要点（図全体で 1〜2 回）。それ以外は null。
   - actions: 画面で何を光らせるか。座標は書かず、番号かデータの値で指示する:
       highlight_x_axis / highlight_y_axis（panel, side=bottom|left|right, labels=[軸題の L 番号]）
       spotlight_curve（curve=C 番号）
       circle_peak（peak=K 番号 を優先。候補に無いピークなら curve と x_range=[下限,上限] と extremum=max|min）, label に表示する文字（例 "353 °C"。本文に数値があれば本文の値）
       band_x（panel, x_range=[下限,上限]・横軸の値）, band_y（panel, y_range, axis）
       box_label / zoom_label（labels=[L・T・G 番号]）, zoom_panel（panel）
       arrow_norm（labels=[出発の G 番号, 行き先の G 番号] なら部品から部品への矢印。番号が無ければ from=[x,y], to=[x,y] を図全体 0〜1000 で）
       候補の番号が無い図だけ: box_norm（box=[x0,y0,x1,y1]・図全体を 0〜1000 とした値）
     使わない引数は null（labels は []）。
   - evidence: そのコマの主張の根拠。source は "caption" か "para:<番号>"（表なら "table" も）、quote は原文から**そのまま**写した連続した一部（20〜120 字）。
     根拠が一般知識（例「DTG は重さの減る速さ」）なら kind=background にして evidence は []。
     本文やキャプションに書かれていない解釈・数値を作らない。本文の数値と図から読んだ値が食い違うなら本文の値を使い、unsure に書く。
3. summary_ja: この図が示すことを 1 文。takeaways_ja: 今日のまとめ 3 項目（各 25 字以内）。

JSON だけを返してください。
"""


# ---- 意味の検査 -----------------------------------------------------------

def _norm_space(s: str) -> str:
    s = s.replace("­", "").replace("◦", "°")
    return re.sub(r"\s+", " ", s).strip().lower()


MIN_QUOTE = 12  # 正規化後の引用の最短の長さ（"TG" や " " のような何にでも一致する引用を根拠と認めない）


def match_key(s: str) -> str:
    """引用の照合用: 全角の数字・記号を半角に揃え（NFKC）、空白を全部消す。
    原文は改行の位置に空白が入り（日本語の「関与 して」、英語の行末ハイフン「indi- cates」）、
    モデルは数字を全角で写すことがあり、本当に原文にある引用が [要確認] になっていた
    （2026-11-10 の結論で nishikiori2015 の 5 ステップ・lee1990 の 1 ステップ）。"""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _norm_space(s)))


def quote_ok(quote: str, key_src: str) -> bool:
    """引用が原文（match_key 済み）に実在するか。長さは今までどおり空白を 1 つに詰めた形で測る
    （空白を消したぶん短い引用が通るようにはしない）。図・概要・結論の照合はみなこれを使う。"""
    return len(_norm_space(quote)) >= MIN_QUOTE and match_key(quote) in key_src


def _numbers(text: str) -> set[str]:
    """文の中の数値（2 桁以上か小数）を取り出す。1 桁は「2 つの線」のような言い回しと区別できないので見ない。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)  # 1,000 → 1000（カンマの後ろだけ拾うと 000 になる）
    return {m for m in re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d])", t) if len(m) >= 2 or "." in m}


def unsupported_numbers(step: dict, sources: dict[str, str], tick_values: set[str]) -> list[str]:
    """ナレーション・字幕・注記のラベルにある数値のうち、本文・キャプション・目盛りの読みのどこにも無いもの。

    Codex レビュー（2026-10-28）: 引用が実在しても、説明の数値が作られていれば検査をすり抜けた。
    kind=background でも数値は照合する（「一般知識」の札で捏造の数値が通らないように）。
    """
    import unicodedata
    pool = unicodedata.normalize("NFKC", " ".join(sources.values()))
    have = _numbers(pool) | tick_values
    text = " ".join([step.get("say", ""), step.get("caption_html", "") or ""] +
                    [a.get("label") or "" for a in step.get("actions", [])])
    def known(n: str) -> bool:
        # 小数の末尾の 0 だけ同一視する（350.0 = 350）。整数に rstrip("0") を掛けると、本文の [35] で 350 が
        # 通ってしまった（Fable 5.1 の二段目レビュー 2026-10-28）
        if n in have:
            return True
        return "." in n and n.rstrip("0").rstrip(".") in have
    return sorted(n for n in _numbers(text) if not known(n))


def tick_values_of(a: dict) -> set[str]:
    """LLM が読んだ目盛りの値（図に書かれている数値なので、説明に使ってよい）。"""
    out = set()
    for p in a.get("panels", []):
        axes = p.get("axes", [])
        items = axes.values() if isinstance(axes, dict) else axes
        for ax in items:
            ticks = ax.get("ticks") or {}
            vals = ticks.values() if isinstance(ticks, dict) else [t.get("value") for t in ticks]
            for v in vals:
                if v is not None:
                    out.add(str(int(v)) if float(v).is_integer() else str(v))
    return out


def _union(boxes: list) -> list | None:
    """外接矩形をまとめた外接矩形（空なら None）。"""
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def check_analysis(a: dict, geom: dict, sources: dict[str, str]) -> list[str]:
    """番号の実在・軸の所属・引用の実在を検査し、問題の一覧を返す（空なら合格）。

    sources: {"caption": キャプション原文, "para:p12": 段落原文, ...}
    """
    errs: list[str] = []
    ticks = {t["id"]: (p["id"], t["side"]) for p in geom.get("panels", []) for t in p["ticks"] if t["id"]}
    curves = {c["id"]: p["id"] for p in geom.get("panels", []) for c in p["curves"]}
    peaks = {k["id"] for p in geom.get("panels", []) for c in p["curves"] for k in c.get("peaks", [])}
    labels = {l["id"] for p in geom.get("panels", []) for l in p["labels"]} | {g["id"] for g in geom.get("regions", [])}
    panels = {p["id"] for p in geom.get("panels", [])}
    side_ok = {"x": {"bottom"}, "x_top": {"top"}, "y_left": {"left"}, "y_right": {"right"}}
    for p in a.get("panels", []):
        if panels and p["id"] not in panels:
            errs.append(f"panels: {p['id']} は存在しないパネル")
        for ax in p.get("axes", []):
            for t in ax.get("ticks", []):
                if t["id"] in labels and t["id"].startswith("L"):
                    continue  # 目盛りの線と結び付かなかった数字の塊を目盛りとして読むのは許す（位置は塊の中心）
                if t["id"] not in ticks:
                    errs.append(f"axes.{ax['axis']}: 目盛り {t['id']} は存在しない")
                elif ticks[t["id"]][1] not in side_ok.get(ax["axis"], set()):
                    errs.append(f"axes.{ax['axis']}: 目盛り {t['id']} は {ticks[t['id']][1]} 側の目盛り（軸の取り違え）")
        for c in p.get("curves", []):
            if c["id"] not in curves:
                errs.append(f"curves: {c['id']} は存在しない曲線")
    norm_src = {k: match_key(v) for k, v in sources.items()}
    tv = tick_values_of(a)
    # 番号 → 外接矩形（PDF 座標）。寄る範囲と囲む所の食い違いを見るのに使う
    bbox_of = {g["id"]: g["bbox"] for g in geom.get("regions", [])}
    bbox_of.update({l["id"]: l["bbox"] for p in geom.get("panels", []) for l in p["labels"]})
    for i, st in enumerate(a.get("steps", [])):
        for act in st.get("actions", []):
            if act.get("panel") and act["panel"] not in panels:
                errs.append(f"steps[{i}]: panel={act['panel']} は存在しない（灰色の札の P 番号。パネルが無い図では null）")
            for key, pool in (("curve", curves), ("peak", peaks)):
                v = act.get(key)
                if v and v not in pool:
                    errs.append(f"steps[{i}]: {key}={v} は存在しない")
            for l in act.get("labels") or []:
                if l not in labels and l not in ticks:
                    errs.append(f"steps[{i}]: labels の {l} は存在しない")
            if act["type"] == "circle_peak" and not act.get("peak") and not (act.get("curve") and act.get("x_range")):
                errs.append(f"steps[{i}]: circle_peak には peak か curve＋x_range が要る")
        # 寄った画面の外を囲む指示は、どちらかの番号の取り違え（Jia 2017 Fig.4: G8 に寄りながら G22 を囲んだ）
        zb = _union([bbox_of[l] for a_ in st.get("actions", []) if a_["type"] == "zoom_label"
                     for l in a_.get("labels") or [] if l in bbox_of])
        if zb:
            for a_ in st.get("actions", []):
                if a_["type"] != "box_label":
                    continue
                for l in a_.get("labels") or []:
                    b = bbox_of.get(l)
                    if b and (b[2] < zb[0] or b[0] > zb[2] or b[3] < zb[1] or b[1] > zb[3]):
                        errs.append(f"steps[{i}]: box_label の {l} は zoom_label で寄る範囲の外にあり画面に映らない"
                                    "（話している所の番号を選び直す）")
        for ev in st.get("evidence", []):
            src = norm_src.get(ev["source"])
            if src is None:
                errs.append(f"steps[{i}]: 根拠の出典 {ev['source']} は存在しない（caption か para:番号。表なら table）")
            elif len(_norm_space(ev["quote"])) < MIN_QUOTE:
                errs.append(f"steps[{i}]: 引用「{ev['quote']}」が短すぎる（{MIN_QUOTE} 字以上の連続した原文を写す）")
            elif not quote_ok(ev["quote"], src):
                errs.append(f"steps[{i}]: 引用「{ev['quote'][:40]}…」が {ev['source']} の原文に見つからない（原文をそのまま写す）")
        bad = unsupported_numbers(st, sources, tv)
        if bad:
            errs.append(f"steps[{i}]: 数値 {', '.join(bad)} が本文・キャプション・目盛りのどこにも無い（本文に無い数値を作らない）")
    return errs


def normalize(a: dict) -> dict:
    """スキーマ向けの配列表現を、resolve が読む辞書表現（契約 v1）に直す。"""
    out = dict(a)
    panels = []
    for p in a.get("panels", []):
        if isinstance(p.get("axes"), dict):
            panels.append(p)  # すでに辞書表現（手書きの analysis.json など）
            continue
        axes = {ax["axis"]: {"title": ax["title"], "unit": ax["unit"], "meaning_ja": ax["meaning_ja"],
                             "ticks": {t["id"]: t["value"] for t in ax["ticks"] if t["value"] is not None}}
                for ax in p.get("axes", [])}
        curves = {c["id"]: {"name": c["name"], "axis": c["axis"]} for c in p.get("curves", [])}
        panels.append({"id": p["id"], "axes": axes, "curves": curves, "unsure": p.get("unsure", [])})
    out["panels"] = panels
    return out


def geom_signature(geom: dict) -> str:
    """幾何解析の番号と位置から作る短い署名（番号の振り直しを検出するため）。"""
    import hashlib
    parts = []
    for p in geom.get("panels", []):
        parts.append(p["id"] + str([round(v, 1) for v in p["frame"]]))
        parts += [f"{t['id']}:{t['side']}:{round(t['pos'], 1)}" for t in p["ticks"] if t["id"]]
        # 同じ色・同じ要素数の曲線が入れ替わっても気付けるよう、中心線の点を数点まぜる
        parts += [f"{c['id']}:{c['color']}:{c['n_items']}:" +
                  ",".join(f"{q[0]:.0f}/{q[3]:.0f}" for q in c["profile"][::max(1, len(c["profile"]) // 8)])
                  for c in p["curves"]]
        parts += [f"{k['id']}:{round(k['x_pt'], 1)}" for c in p["curves"] for k in c.get("peaks", [])]
        parts += [f"{l['id']}:{[round(v) for v in l['bbox']]}" for l in p["labels"]]
    parts += [f"{g['id']}:{[round(v) for v in g['bbox']]}" for g in geom.get("regions", [])]
    # 切り出し範囲: ラスター図は番号の付く部品がほとんど無く、切り出しを直しても署名が変わらず、
    # 古い画像に対する答え（box_norm は画像に対する比）を使い回していた（Jia 2017 Fig. 2 で柱を外したとき）
    parts.append("clip" + str([round(v) for v in geom.get("clip", [])]))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


# ---- 呼び出し口 -----------------------------------------------------------

class DriverError(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind  # timeout | permission | quota | invalid_json | process


def _strip_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s


def _run_with_tree_kill(cmd: list[str], stdin, timeout: int):
    """cmd を実行し、時間切れなら子プロセスごと止める。返り値は CompletedProcess か None（時間切れ）。

    Windows で shell=True の subprocess.run(timeout=…) は、時間切れでシェルだけ止めて、
    孫の codex が出力の管を握ったまま動き続けるので、終わるまで待ち続けてしまう
    （実測 2026-10-28: 15 分の設定で 70 分待った）。taskkill /T で木ごと止める。
    """
    import shutil
    exe = shutil.which(cmd[0]) or cmd[0]  # codex.cmd などの実体を探す
    p = subprocess.Popen([exe, *cmd[1:]], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         shell=False)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        return None
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def call_codex(prompt: str, images: list[Path], schema_path: Path, out_path: Path, timeout: int = 900) -> dict:
    """codex exec に画像を添付して呼ぶ。-m は付けない（config.toml で一元管理する教訓）。"""
    prompt_file = out_path.with_suffix(".prompt_sent.md")
    prompt_file.write_text(prompt, encoding="utf-8")
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check",
           "-c", "model_reasoning_effort=medium",
           "--output-schema", str(schema_path), "-o", str(out_path)]
    for im in images:
        cmd += ["-i", str(im)]
    cmd.append("-")
    with open(prompt_file, "rb") as fin:
        r = _run_with_tree_kill(cmd, fin, timeout)
    if r is None:
        raise DriverError("timeout", f"codex が {timeout} 秒で終わらなかった")
    log = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    out_path.with_suffix(".codex.log").write_text(log[-20000:], encoding="utf-8")
    if r.returncode != 0 or not out_path.exists():
        kind = "quota" if re.search(r"rate limit|usage limit|quota", log, re.I) else "process"
        raise DriverError(kind, f"codex rc={r.returncode}: {log[-400:]}")
    try:
        return json.loads(_strip_fence(out_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise DriverError("invalid_json", str(e))


def call_claude(prompt: str, images: list[Path], schema_path: Path, out_path: Path, timeout: int = 900) -> dict:
    """claude -p に依頼する。画像は依頼文の中の絶対パスを Read させる（--add-dir は添付ではない）。"""
    dirs = sorted({str(p.parent.resolve()) for p in images})
    cmd = ["claude", "-p", "-", "--output-format", "json", "--allowedTools", "Read",
           "--json-schema", schema_path.read_text(encoding="utf-8")]
    for d in dirs:
        cmd += ["--add-dir", d]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace", shell=(os.name == "nt"))
    except subprocess.TimeoutExpired:
        raise DriverError("timeout", f"claude が {timeout} 秒で終わらなかった")
    out_path.with_suffix(".claude.log").write_text((r.stdout or "")[-20000:] + "\n---\n" + (r.stderr or "")[-5000:], encoding="utf-8")
    if r.returncode != 0:
        kind = "permission" if "permission" in (r.stdout + r.stderr).lower() else "process"
        raise DriverError(kind, f"claude rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    try:
        wrapper = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise DriverError("invalid_json", f"claude の出力が JSON でない: {e}")
    # --json-schema のときは structured_output に入る。無ければ result を JSON として読む
    data = wrapper.get("structured_output")
    if data is None:
        try:
            data = json.loads(_strip_fence(wrapper.get("result", "")))
        except json.JSONDecodeError as e:
            raise DriverError("invalid_json", f"structured_output も result の JSON も無い: {e}")
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


API_MODEL = os.environ.get("PFE_API_MODEL", "claude-opus-5-5")
ENV_FILE = Path.home() / ".config" / "pfe" / "env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """~/.config/pfe/env の `名前=値` を、まだ設定されていない環境変数にだけ入れる（鍵は施主が書く。値は表示しない）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _first_answer_object(text: str) -> dict | None:
    """文字列に並んだ JSON オブジェクトを先頭から読み、steps を持つ最初のものを返す（無ければ None）。"""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and "steps" in obj:
            return obj
        i = text.find("{", end)
    return None


def call_api(prompt: str, images: list[Path], schema_path: Path, out_path: Path, timeout: int = 900, model: str | None = None) -> dict:
    """Claude API に画像 2 枚を添付して呼ぶ（施主 2026-10-30「Claude の API は使ってよい」）。
    鍵は環境変数 ANTHROPIC_API_KEY か ~/.config/pfe/env（コードに書かない）。
    答えの形はスキーマを依頼文に添えて頼み、検査は analyze() のスキーマ＋意味の検査に任せる
    （structured outputs は SCHEMA が大きすぎて「compiled grammar is too large」で 400 になった・2026-10-30）。
    安全側の判定で断られたときは、サーバ側の fallbacks で別のモデルが続ける。"""
    import base64
    import anthropic
    content = []
    for im in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": base64.standard_b64encode(im.read_bytes()).decode("ascii")}})
    content.append({"type": "text", "text": prompt + "\n\n## 返す JSON の形（JSON Schema）\n"
                    "次のスキーマに合う JSON オブジェクトを 1 つだけ返す。``` で囲まず、前後に説明を書かない。\n"
                    + schema_path.read_text(encoding="utf-8")})
    out_path.with_suffix(".prompt_sent.md").write_text(prompt, encoding="utf-8")
    load_env_file()
    client = anthropic.Anthropic(timeout=timeout, max_retries=2)
    try:
        with client.beta.messages.stream(
            model=model or API_MODEL, max_tokens=64000,     # model: 文字だけの段は TEXT_MODEL（pfe/overview.py）
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise DriverError("permission", f"API キーが無いか無効（ANTHROPIC_API_KEY）: {e}")
    except anthropic.RateLimitError as e:
        raise DriverError("quota", f"API の利用上限: {e}")
    except anthropic.APITimeoutError:
        raise DriverError("timeout", f"API が {timeout} 秒で終わらなかった")
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        raise DriverError("process", f"API の呼び出しに失敗: {e}")
    u = msg.usage
    # 1 呼び出し 1 行で追記する（上書きだと頼み直しの分が費用の集計から消える）
    with out_path.with_suffix(".api.log").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            # stream の最終メッセージには _request_id が無い（ParsedBetaMessage）。id で照合できる
            "model": msg.model, "stop_reason": msg.stop_reason, "id": msg.id,
            "request_id": getattr(msg, "_request_id", None),
            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens}, ensure_ascii=False) + "\n")
    if msg.stop_reason == "refusal":
        raise DriverError("process", f"API が答えを断った: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise DriverError("invalid_json", "答えが max_tokens で切れた")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text.startswith("{") and "{" in text:      # ``` や前置きが付いたときは最初の { から最後の } まで
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # JSON の後ろに 2 つ目のオブジェクトや説明が付いたとき（Sapouna Fig.10 で "Extra data"・2026-10-31）は、
        # 先頭から順に読んで答えの形（steps を持つ）のものを使う。読めなければ返答の本文を残して失敗にする
        data = _first_answer_object(text)
        if data is None:
            out_path.with_suffix(".bad.txt").write_text(text, encoding="utf-8")
            raise DriverError("invalid_json", f"API の答えが JSON でない: {e}")
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def analyze(fig: dict, geom: dict, sources: dict[str, str], mentions: list[dict], refs_for_fig: list[dict],
            som_path: Path, clean_path: Path, out_dir: Path, driver: str = "codex") -> dict:
    """図 1 枚の解析を回す。返り値 {"status": ok|failed|manual, "analysis": ..., "errors": [...], "attempts": n}。"""
    fid = fig["id"]
    schema_path = out_dir / "analysis.schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    prompt = build_prompt(fig, geom, mentions, refs_for_fig, som_path.resolve(), clean_path.resolve(),
                          inline_images=(driver in ("codex", "api")))
    # 注記の判定（Jev）の指摘と前回の答え。scripts/fix_marks.py が置き、作り直しのあとで片付ける
    feedback = out_dir / f"{fid}.feedback.md"
    if feedback.exists():
        prompt = prompt + "\n\n" + feedback.read_text(encoding="utf-8")
    (out_dir / f"{fid}.prompt.md").write_text(prompt, encoding="utf-8")
    raw_path = out_dir / f"{fid}.analysis.raw.json"
    final_path = out_dir / f"{fid}.analysis.json"

    if driver == "manual":
        if final_path.exists():
            a = json.loads(final_path.read_text(encoding="utf-8"))
            return {"status": "ok", "analysis": a, "errors": check_analysis(a, geom, sources), "attempts": 0}
        return {"status": "manual", "analysis": None, "errors": [f"{final_path} を書いてから再実行"], "attempts": 0}

    call = {"codex": call_codex, "api": call_api}.get(driver, call_claude)
    # 生の答えがどの番号づけに対するものかを横に残す（run.py の回収はこれが一致したときだけ使う）
    # 前の答えを先に消す: 今回の呼び出しが時間切れで終わると、古い番号づけの答えが新しい署名と並んで残り、
    # 次の run.py で回収されてしまう（Fable 5.1 の二段目レビュー 2026-10-28）
    raw_path.unlink(missing_ok=True)
    (out_dir / f"{fid}.analysis.raw.sig").write_text(geom_signature(geom), encoding="utf-8")
    images = [som_path, clean_path]
    errors: list[str] = []
    a = None
    for attempt in (1, 2):
        t0 = time.time()
        try:
            a = call(prompt, images, schema_path, raw_path)
        except DriverError as e:
            return {"status": "failed", "analysis": None, "errors": [f"{e.kind}: {e}"], "attempts": attempt}
        errors = check_analysis(a, geom, sources)
        print(f"  [{fid}] {driver} 応答 {time.time() - t0:.0f} 秒・検査の指摘 {len(errors)} 件")
        if not errors:
            break
        if attempt == 1:
            # 指摘を添えて 1 回だけ直してもらう
            prompt = prompt + "\n\n## 前回の答えの問題（直して、全体を JSON で返し直してください）\n" + \
                "\n".join(f"- {e}" for e in errors) + "\n\n前回の答え:\n" + json.dumps(a, ensure_ascii=False)
    final = normalize(a)
    # どの幾何解析（番号の振り方）に対する答えかを記録する。幾何を作り直して番号が変わったら、
    # run.py がこの署名の不一致を見て解析をやり直す（古い答えが別の曲線を黙って指すのを防ぐ）
    final["geom_sig"] = geom_signature(geom)
    final_path.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"status": "ok", "analysis": final, "errors": errors, "attempts": attempt}
