# データ契約 v1（段と段の間のファイル）

設計の背景は `design_v0.md`、Codex のレビュー反映は本書末尾。座標の単位を必ず書く（pt = PDF ポイント、px = 図 PNG の画素）。
Python は `py -3.13`、PDF は `import pymupdf`（`fitz` は使わない）。

## 置き場

```
work/<paper_id>/
  source.pdf                 入力の写し
  meta.json                  {title, authors[], journal, year, doi, source_path_or_url}
  biblio.json                Crossref で確かめた {doi, title, authors[{family, given}], journal, year}（任意。fill_biblio.py。棚と検索は meta.json より優先）
  text.json                  本文の段落・見出し・図の言及（text.py）
  figures.json               図の一覧と切り出し範囲（figures.py）
  figures_override.json      人手の修正（任意。あれば figures.json より優先）
  refs.json                  参考文献リストと図ごとの引用（cite.py）
  fig/<fig_id>.png           図の切り出し（長辺 1800px 目安）
  fig/<fig_id>.geom.json     幾何解析（geometry.py）
  fig/<fig_id>.som.png       番号札つき画像（som.py）
  fig/<fig_id>.prompt.md     LLM への依頼文（analyze.py）
  fig/<fig_id>.analysis.json LLM の答え（analyze.py。手で書いてもよい）
  fig/<fig_id>.resolved.json 画素の図形（resolve.py）
  audio/<fig_id>_<n>.mp3     ナレーション（tts.py）
  bundle.json                再生ページの入力（build.py）
  player.html                単一 HTML（build.py。bundle と画像を埋め込む）
```

`paper_id` は入力ファイル名から作る短い英数字（例 `taira2026_jaap`）。

## figures.json（figures.py）

```jsonc
[{ "id": "fig1", "number": "1", "label": "Fig. 1",      // id は安定（番号から作る）。Scheme/Table は "scheme1" 等
   "page": 2,                                            // 1 始まり
   "caption": "Fig. 1. TG and DTG curves of ...",        // キャプション全文（改行は空白に）
   "caption_bbox": [x0,y0,x1,y1],                        // pt
   "clip": [x0,y0,x1,y1],                                // pt。図の本体（キャプションは含めない）
   "layout": "left|right|full",                          // 段組のどこか
   "confidence": "high|low",                             // 自動の切り出しに自信があるか
   "rotate": 270,                                        // 任意。中身が回っている図表だけ（90/180/270）。page・clip・caption_bbox は常に元のページの座標。
                                                         // 画像と幾何は pfe/orient.py の figure_view が返す正立側の page・clip で作る
   "continues": "fig7",                                  // 任意。続きのページの図だけ（id は "fig7-cont"、札は「Fig. 7（続き）」）。caption は親の本文＋" (continued)"、
                                                         // 本文の言及と引用は親のものを使う（pfe/spanning.py）
   "source": "auto|override" }]
```

## text.json（text.py）

```jsonc
{ "sections": [{ "id": "s3_1", "title": "3.1. TG-MS analysis" }],
  "paragraphs": [{ "id": "p12", "section": "s3_1", "page": 2,
      "text": "The TG and DTG curves ... respectively. Since ...",   // ページ跨ぎ・段跨ぎは連結済み・ハイフン改行は戻す
      "sentences": [{ "i": 0, "text": "...", "figs": ["fig1"], "cites": [12, 13] }] }],
  "fig_mentions": { "fig1": [{ "para": "p12", "sentence": 0 }] } }
```

- キャプション・柱（ヘッダー/フッター）・ページ番号・参考文献リストは段落に含めない。
- 図の言及は `Fig. 1` `Figs. 2 and 3` `Fig. 5a` `Figures 2–4` `Fig. S2`（S は補足として `figS2`）を拾う。範囲は展開する。

## refs.json（cite.py）

```jsonc
{ "style": "numeric|author_year",
  "entries": [{ "num": 12, "raw": "<文献リストの原文>", "doi": "10.xxxx/...", "title": "...", "year": 2008,
                "first_author": "Kawamoto", "url": "https://doi.org/...",
                "abstract": null, "oa_url": null,               // --online のときだけ Crossref/OpenAlex で埋める
                "label": "Wang 2023a" }],                       // author_year だけ。番号の代わりに画面に出す札（num は一覧での順番）
  "by_figure": { "fig1": [{ "num": 12, "para": "p12", "sentence": 0,
                             "context": "<その引用を含む文の原文>" }] } }
```

- **主張ごとの対応**（Codex 指摘）: 段落全体の引用を図に付けない。図に言及した文と、その直後の 1 文に現れた引用だけを `by_figure` に入れる。

## analysis.json（LLM の答え。analyze.py が JSON Schema で検査）

```jsonc
{ "fig_type": "line_plot|bar|chromatogram|spectrum|scheme|structure|photo|table_like|multi",
  "summary_ja": "<この図が示すことを 1 文>",
  "panels": [{ "id": "P1",
     "axes": { "x": { "title": "Temperature", "unit": "°C", "meaning_ja": "...", "ticks": { "T12": 100 } },
               "y_left": {...}, "y_right": {...} },
     "curves": { "C1": { "name": "MWL TG", "axis": "y_left" } },
     "unsure": ["<読めなかった・迷ったこと>"] }],
  "steps": [{ "say": "<ナレーション 1〜3 文>", "caption_html": "<字幕。強調語を <em> で>",
     "point": null,                                         // ★POINT カードの文言（任意・12〜20 字）
     "kind": "fact|background",
     "actions": [ { "type": "...", ... } ],                  // 下の表
     "evidence": [{ "source": "caption|para:p12", "quote": "<原文の連続した一部>" }] }],
  "takeaways_ja": ["<今日のまとめ 1>", "<2>", "<3>"] }
```

actions（座標は番号かデータ値。画素は書かない）:

| type | 引数 | 意味 |
|---|---|---|
| highlight_x_axis / highlight_y_axis | panel, side, labels[] | 軸の線＋目盛り数字＋軸題（L 番号）を光らせる |
| spotlight_curve | curve | 曲線をなぞって光らせる（他は暗く） |
| circle_peak | curve, peak（K 番号）or x_range＋extremum, label | ピークに赤丸。K 番号を優先 |
| band_x / band_y | panel, x_range / y_range＋axis, label | 黄色の帯 |
| box_label / zoom_label | labels[] | 図中ラベルを囲む / 寄る |
| zoom_panel | panel | パネルに寄る |
| box_norm / arrow_norm | box / from,to（図全体 0〜1000 の正規化座標）, as | 候補が無い図の退避路 |

## resolved.json（resolve.py）

```jsonc
{ "calibration": { "P1": { "x": {"a":..,"b":..,"log":false,"resid":..}, "errors": {} } },
  "steps": [{ "index": 0, "warnings": [],
     "shapes": [ {"kind":"band","box":[x0,y0,x1,y1]},
                 {"kind":"circle","center":[x,y],"r":30,"label":"353 °C","value":{"x":354.2,"y":2.57},"color":"#e87132"},
                 {"kind":"trace","points":[[x,y],...],"color":"#00af50"},
                 {"kind":"axis","box":[...],"line":[[x,y],[x,y]]},
                 {"kind":"box","box":[...]}, {"kind":"zoom","box":[...]},
                 {"kind":"arrow","from":[x,y],"to":[x,y]} ] }] }
```
px は `fig/<id>.png` の画素。`approx: true` は記録のみ（画面に札は出さない。施主 2026-10-30「なぞに近似っていう文字が出ているのが気になる」）。縦長の図は注記の範囲へ自動で寄る（player の autoView）。

## bundle.json（build.py → player）

```jsonc
{ "paper": { "title": "...", "authors": ["..."], "journal": "...", "year": 2026, "doi": "...", "source": "..." },
  "figures": [{
     "id": "fig1", "label": "Fig. 1", "caption": "...", "img": "<data URL か相対パス>", "width": 1800, "height": 1080,
     "summary_ja": "...", "takeaways_ja": ["..."], "approx": false,
     "steps": [{ "say": "...", "caption_html": "...", "point": null, "kind": "fact|background|unverified",
                 "shapes": [ ...resolved の図形... ], "warnings": [],
                 "evidence": [{ "source": "para:p12", "where": "3.1 TG-MS analysis 第1段落", "quote": "...", "verified": true }],
                 "audio": "<data URL か相対パス か null>", "duration": 6.2 }],
     "refs": [{ "num": 12, "raw": "...", "doi": "...", "url": "...", "title": "...", "year": 2008,
                "context": "<図の言及文>", "abstract": null, "oa_url": null }] }] }
```

- `kind: "unverified"` は evidence の quote が原文に見つからなかったステップ（画面に [要確認]）。
- `duration` は音声の長さ（秒）。音声が無ければ `null`（player が文字数から見積もる）。

## Codex レビュー（2026-10-28）の反映

- ピークも候補番号（K1..）にして LLM に選ばせる。曖昧（候補が近接・端に張り付き・較正失敗）なら吸着せず warnings に残す。
- 曲線は色でなく**パス単位**で分ける（実装済み）。交差・破線の欠落は不確定扱い。
- 図の切り出しは自動の候補＋`figures_override.json` で人手修正できるようにする。
- LLM 呼び出しは交換可能な境界（claude: `--json-schema` / codex: `-i` と `--output-schema` / manual）。タイムアウト・権限・上限は JSON 不正と分けて記録。
- 引用文の実在は説明の正しさを保証しない → 主張（ステップ）ごとに evidence を持たせ、位置精度は本文値と人手の正解で別々に評価する。
