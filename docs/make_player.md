# 論文 PDF から動く解説を作る手順（Claude Code のセッション用）

API キーを使わず、自分の Claude プランの枠で `player.html` を作る。対象の PDF は依頼文に書いてある。座標はプログラムが計算し、このセッションは図を読んで `analysis.json` を書く役を担う。

## コマンド
以下の `PY` は、Windows なら `py -3.13`、macOS・Linux なら `python3` に置き換える。リポジトリの一番上のフォルダで実行する。処理に時間がかかるので timeout は長めにする。

## 手順
1. `PY scripts/check_env.py` を実行する。足りないものがあれば、そこで止めて表示された内容を伝える
2. `PY scripts/run.py --pdf "<PDF のパス>" --driver manual --no-tts` を実行する。最後の行の `work/<pid>/player.html` から `<pid>` を読み取る。番号札つき画像 `fig/<id>.som.png` と依頼文 `fig/<id>.prompt.md` ができる
3. 図表ごとに `work/<pid>/fig/<id>.prompt.md` を読み、指示どおり `<id>.som.png` と `<id>.png` を見て `<id>.analysis.json` を書く。形は `fig/analysis.schema.json` が正しい
4. `PY scripts/run.py --pdf work/<pid>/source.pdf --id <pid> --driver manual --no-tts` を実行する。検査の指摘が出た図の `analysis.json` を原文に沿って直す。**直した図は `geom_sig` の行を消してから実行しないと、検査が走らない**
5. 図表がそろうと `fig/overview.prompt.md` ができる。指示どおり `fig/overview.analysis.json` を書く。引用はアブストラクトからそのまま写す。`PY scripts/check_overview.py <pid>` で検査する
6. `PY scripts/run.py --pdf work/<pid>/source.pdf --id <pid> --driver manual` で音声と `player.html` を作る
7. `PY scripts/bulk_check.py sheets <pid>` を 1 回実行し、`work/_batch/sheets/<pid>_*.png`（注記を図に重ねた一覧）を見て、ずれた所だけ直す。直したら 6 をもう一度実行する
8. `PY scripts/bulk_check.py after <pid>` を実行して、残った指摘を報告に含める

依頼文の中のパスが `/mnt/<ドライブ>/…` のときは、WSL の表記なので Windows の表記に読み替えて開く。

## アブストラクトの確認
`work/<pid>/abstract.json` の `text` が概要の根拠になる。先に PDF と見比べる。**所属の文になっている・途中で切れている・記号が抜けている・空のとき**は、PDF から正しい全文を写して直し、`page` も合わせる。直したことは報告する。アブストラクトの無い論文は概要を作らず、報告に書く。

## 根拠の段落が足りないとき
検査が引用を認めるのは、その図の `fig_mentions` に載った段落とキャプションだけ。大事な説明をこの理由で削らない。`PY scripts/add_mentions.py <pid> fig1=p15,p16` で段落の本文を確かめ、その図の説明なら `--write` を付けて足す。足した段落は報告に書く。

## 守ること
- 座標を自分で計算しない。番号札（T・C・K・L・G）で答える
- 原文（キャプション・本文・表）に無い数値や解釈をナレーションに入れない。引用は原文からそのまま写す。著者の仮説は仮説として言う
- 図に載せてよいのは、図の一部を指す注記（帯・赤丸・矢印・枠・短い札）だけ。大きな文字やカードを重ねない
- 原文同士の食い違いは断定せず、報告で知らせる
- 変更してよいのは `work/<pid>/` の中だけ。`pfe/`・`scripts/` のコード、`figures.json`、ほかの論文のフォルダは変更しない。Git を操作しない
- 確認用のファイルが要るときは `work/<pid>/_tmp/` に置く
- 書き込みや実行が拒否されたら迂回せず、そこで止めて報告する

## 使用量を抑える
- ブラウザで開かない。一時サーバーを立てない
- 枠の確認は手順 7 の 1 回にまとめ、図ごとに画像を作らない
- `panels` の `axes`・`ticks`・`curves` はリストで書く（辞書だと落ちる）。点の札が使えないときは G 番号か `box_norm` で指し、`panels` は `[]` にする
- 図の中の細かい読み取りに深入りせず、本文とキャプションに書かれていることを説明する

## 報告（15 行以内）
できた図表の数、`player.html` のパス、検査の指摘が残った図、`[要確認]` のステップ数、切り出しの問題に気づいた図、手順どおりにいかなかった所。切り出しの枠が崩れている図は直さず、報告だけする。
