# 論文の図の動く解説（paper-figure-explainer）

Turns the figures and tables of a research paper (PDF) into a narrated walkthrough in a single HTML file: highlights, red circles and subtitles point at what to read. The UI and narration are in Japanese.

論文 PDF の図・表ごとに、軸の意味と読みどころを枠・赤丸・字幕・音声で示す解説（`player.html`）を作ります。

## 仕組み

```
PDF → 図の位置 + 本文・図の言及 + 参考文献
    → 枠・目盛り・曲線・ピーク候補・部品（PDF の描画命令から計算）→ 番号札つきの画像
    → LLM は番号と値だけ答える → 較正・ピーク吸着で画素の図形に変換
    → 根拠の照合 → 音声（edge-tts）→ player.html
```

- **座標を LLM に言わせません。** 目盛り・曲線・ピーク・図中の文字・部品に番号を振った画像を見せ、LLM は「T3=200」「K2 に赤丸」のように番号と値だけ答えます。画素はプログラムが計算します
- **根拠を照合します。** 各ステップの引用が原文に実在しなければ `[要確認]` が付きます
- **図の上に大きな文字を載せません。** 図に載るのは図の一部を指す注記（帯・赤丸・矢印・枠・短い札）だけです

設計は [docs/design_v0.md](docs/design_v0.md)、段と段の間のファイルの形は [docs/contracts_v1.md](docs/contracts_v1.md) にあります。

## 準備（最初の 1 回だけ）

必要なもの:
- Claude の Pro 以上のプラン（**自分のアカウント**。プランの共有はできません）
- [Claude Code](https://code.claude.com/docs)
- Python 3.13
- Git

Windows の PowerShell で、1 行ずつ実行します。macOS・Linux は `py -3.13` を `python3` に読み替えてください（作者が確かめた環境は Windows と WSL です。macOS は未確認です）。

```powershell
git clone https://github.com/daijiro22/paper-figure-explainer.git
```

```powershell
cd paper-figure-explainer
```

```powershell
py -3.13 -m pip install -r requirements.txt
```

```powershell
py -3.13 scripts/check_env.py
```

最後の行に `OK` と出れば準備は完了です。`ANTHROPIC_API_KEY` が設定されているという警告が出たら、そのキーを外してください（残っていると従量課金になります）。

## 解説を作る

1. このフォルダで Claude Code を起動し、**論文ごとに新しいセッション**を始めます
2. `/model` で Sonnet を選びます（枠を節約するため）
3. 次のように入力します。途中でコマンドの実行とファイルの書き込みの確認が出るので、内容を見て許可します

   ```text
   docs/make_player.md の手順で "C:\path\to\論文.pdf" の解説を作って
   ```

4. 終わると `work/<論文の id>/player.html` ができます。ブラウザで開くだけで再生できます

PDF の中に書かれた文も Claude が読みます。出所の分からない PDF では、許可の確認を飛ばさないでください。

Sonnet で 1 本 15〜24 万トークンでした（2026-11-06 の実測。図の数で変わります）。普段の Claude の利用と同じ枠を使います。

## 一覧で見る

```powershell
py -3.13 scripts/serve.py
```

http://127.0.0.1:8128/make を開くと、作った解説が棚に並びます。タグでの絞り込み・題の書き換え・原文の画面もここにあります。新しく作った解説は、サーバを再起動すると一覧に出ます。一覧の DB は `~/.local/share/pfe/pfe.db` で、起動時に `work/` から作り直します。

サーバは 127.0.0.1 だけで待ち受け、別のサイトからの書き込みの要求は断ります。

## 任意の機能

**API キーで作る。** `~/.config/pfe/env` に `ANTHROPIC_API_KEY=...` の行を書くと、画面の「PDF を置く」と `scripts/run.py --driver api` が使えます。従量課金です。

**Crossref への連絡先。** 環境変数 `PFE_CONTACT_EMAIL` にメールアドレスを入れると、参考文献の問い合わせの User-Agent に付きます。無くても動きます。

**棚の論文情報を確かめる。** `py -3.13 scripts/fill_biblio.py` で、年・雑誌・著者を Crossref と照合します。

**切り出しと注記の判定。** `scripts/judge_figs.py`・`scripts/judge_marks.py` は Typesafe の Jev を使います（鍵は `TYPESAFE_API_KEY`）。

### 自分たちだけのサイト

`scripts/export_site.py` は、作った解説を Cloudflare Workers に載せる形に組み立てます。**図・アブストラクト・本文の引用・ページ画像が含まれます。** 載せる範囲が著作権と出版社の条件に合うかは、自分で確かめてください。

順番を変えないでください。Access を後にすると、掛けるまでの間は誰でも見られます。

1. `site.example.json` を `site.json` に写し、`worker` と `domain` を自分の値にします
2. Cloudflare Zero Trust で、そのホスト名の全体を守る Access アプリを**先に**作ります
3. `py -3.13 scripts/export_site.py` で組み立て、出力先で `npx wrangler deploy` を実行します
4. ログインしていない状態で `curl -sI https://<domain>/` などを確かめ、全部 302 で Access に飛ぶことを見ます。200 が出たら直ちに Worker を消してください

## 守ること

- **PDF と生成物を GitHub に上げない。** 論文は著作物です。`work/` は Git の対象外になっているので、この設定を変えないでください
- **解説を人に見せる前に中身を確かめる。** 自動の検査は「引用が原文に実在するか」「原文に無い数値が無いか」までで、引用が主張を支えているかは見ていません

## テスト

```powershell
py -3.13 -m pytest tests -q
```

実物の論文を使う回帰テストは、PDF を配れないのでスキップされます。手元に同じ論文があれば、置き場を環境変数 `PFE_TEST_PAPERS` か `tests/papers_root.txt` に書くと動きます（ファイル名の形はテストの中にあります）。

## 困ったとき

- **Windows で pymupdf を読み込めない**（アプリ制御ポリシーで止められる PC があります）: WSL に Python の環境を作って、そこで動かします
- **`py` が見つからない**: Python 3.13 を [python.org](https://www.python.org/downloads/) から入れます
- **枠が足りなくなった**: 枠が戻るのを待ってから、同じ論文でもう一度頼みます。書き終えた図の答えは `work/` に残っています

コードのコメントにある「施主」は、このツールの発注者（作者）のことです。

## ライセンス

[GNU AGPL-3.0-or-later](LICENSE)。Copyright (C) 2026 daijiro22

主な依存のライセンス: PyMuPDF（AGPL-3.0 または Artifex の商用）、mutagen（GPL-2.0-or-later）、edge-tts（LGPL-3.0）、そのほかは MIT・BSD・Apache-2.0 など。edge-tts は Microsoft の公式ライブラリではなく、Edge の読み上げを使う非公式のものです。

作った解説の中の図や文章の権利は、元の論文の権利者にあります。このライセンスはそれらに及びません。
