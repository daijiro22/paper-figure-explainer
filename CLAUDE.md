# paper-figure-explainer

論文 PDF を読み込み、図・表ごとに軸の意味と読みどころを枠・赤丸・字幕・音声で示す解説（`player.html`）を作る。
解説を作る依頼は `docs/make_player.md` の手順に従う。設計は `docs/design_v0.md`、段と段の間のファイルの形は `docs/contracts_v1.md` が正。

## Commands

- 環境の確認: `py -3.13 scripts/check_env.py`
- 実行（PDF）: `py -3.13 scripts/run.py --pdf "<論文.pdf>" [--figs fig1,fig7] [--driver codex|api|claude|manual] [--online] [--no-tts]`
- 実行（URL）: `py -3.13 scripts/run.py --url "<論文ページ or PDF の URL>"`
- テスト: `py -3.13 -m pytest tests -q`（実物の PDF を使うテストは `PFE_TEST_PAPERS` が無ければスキップ）
- Web 版: `py -3.13 scripts/serve.py` → http://127.0.0.1:8128/make（127.0.0.1 以外で待ち受けない）
- 作成済みの player を今のテンプレートで組み直す: `py -3.13 scripts/rebuild_players.py`
- 出来上がり: `work/<paper_id>/player.html`（単一 HTML）

macOS・Linux は `py -3.13` を `python3` に読み替える。

## Architecture

```
PDF/URL → figures(図の位置) + text(本文・図の言及) + cite(参考文献・図ごとの引用)
       → geometry(枠・目盛り・曲線・ピーク候補・部品) → som(番号札つき画像)
       → analyze(LLM: 番号と値とデータ座標だけ答える) → resolve(較正・ピーク吸着 → 画素の図形)
       → tts(edge-tts) → build(根拠の照合・bundle.json) → player.html
```

- **座標を LLM に言わせない**: 目盛り数字（T）・曲線（C）・ピーク候補（K）・図中文字（L）・部品（G）に番号を振った画像を見せ、LLM は番号と値だけ答える。画素は resolve が PDF の描画命令から計算する
- LLM の呼び出し口は交換可能（`pfe/analyze.py`）: codex／claude／api／manual（`fig/<id>.analysis.json` を手で書く）
- 根拠の照合: 各ステップの evidence の quote が原文に実在しなければ `[要確認]`（`pfe/build.py`）
- player は `stateAt(t)` の純関数で描く。テンプレートは `player/parts/` の部品で、`player/build_player.py` が 1 枚の HTML に組み込む。JS の部品は 1 つの `<script>` の中で変数を共有するので、`<script>` を分けず順番も変えない

## 既知の落とし穴

- Elsevier の図は目盛り数字・凡例・軸題が文字でなく輪郭の塗り、曲線も色つきの塗りの輪郭。`get_text` では読めない → 幾何で塊を取り、値は LLM に読ませる
- 同じ色の曲線が TG と DTG の 2 本ある → 曲線は色でなくパス単位で分ける
- ピーク位置は頂上 10% の重心に一本化した（±3 °C が図から読める上限）
- 解析の途中で `pfe/`・`scripts/run.py` を編集すると、次の図が途中の版を読み込む
- 検出を変えたら `scripts/diff_detect.py` で全論文の差を見る（ある型の修正が別の型を壊す）
- player を直すのは `player/parts/`。直したら `scripts/rebuild_players.py`（run.py だと画像の図の解析がやり直しになる）

## Do NOT

- 論文の PDF・図・生成物（`work/`）をコミットしない。著作物
- 論文の本文・キャプションに無い数値や解釈をナレーションに入れない
- 図の上に大きな文字・カードを重ねない。載せてよいのは図の一部を指す注記（帯・赤丸・矢印・枠・短い札）だけ
- 検査が見ているのは「引用が原文に実在するか」「原文に無い数値が無いか」まで。引用が主張を支えているかは人が見る
