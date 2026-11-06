"""この PC で Web 版と解析を動かせるかを確かめる（足りないものと入れ方を出す。鍵の値は表示しない）。

    py -3.13 scripts/check_env.py     （Windows で pymupdf が動かない PC は、WSL の venv の python で同じファイルを動かす）

一覧の DB と鍵は機械ごと。自分のプランで作る人（docs/make_player.md）は API キーが要らないので、解析の口はどれか 1 つあればよい。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import importlib
import os
import shutil
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INSTALL = "py -3.13 -m pip install -r requirements.txt"
# (import 名, pip 名, 最低の版)
MODULES = [("pymupdf", "pymupdf", None), ("edge_tts", "edge-tts", (7, 3, 1)), ("PIL", "pillow", None),
           ("numpy", "numpy", None), ("fastapi", "fastapi", None), ("uvicorn", "uvicorn", None),
           ("mutagen", "mutagen", None), ("httpx", "httpx", None), ("anthropic", "anthropic", None),
           ("jwt", "pyjwt[crypto]", None)]


def _ver(pipname: str) -> tuple:
    """入っている版（pip の登録から読む。モジュールに __version__ が無いものがある）。"""
    from importlib.metadata import version
    try:
        v = version(pipname.split("[")[0])
    except Exception:
        return (0,)
    return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())


def main() -> int:
    ng = 0
    print(f"Python {sys.version.split()[0]}（{sys.executable}）")
    if sys.version_info < (3, 11):
        print("  × Python が古い。Windows では py -3.13 で動かす（python は 3.9 で別の pymupdf が入っている）")
        ng += 1

    for name, pipname, need in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception as e:      # pymupdf は DLL が止められると ImportError 以外で落ちることがある
            print(f"  × {pipname}: 読み込めない（{type(e).__name__}: {str(e)[:80]}）")
            ng += 1
            continue
        if need and _ver(pipname) < need:
            print(f"  × {pipname}: 版が古い（{'.'.join(map(str, _ver(pipname)))}・{'.'.join(map(str, need))} 以上が要る）")
            ng += 1
        else:
            print(f"  ○ {pipname}")

    # シェルの環境に API キーがあると、Claude Code はプランの枠でなく従量課金で動く。
    # 鍵のファイルは Claude Code に読まれないので、ファイルを読み込む前に確かめる
    shell_api_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    from pfe.analyze import ENV_FILE, load_env_file
    load_env_file()
    print(f"鍵のファイル: {ENV_FILE}（{'ある' if ENV_FILE.is_file() else '無い'}）")
    keys = [("ANTHROPIC_API_KEY", "Web 版と --driver api（任意）"), ("TYPESAFE_API_KEY", "Jev の判定（任意）")]
    if (ROOT / "scripts" / "notion_prepare.py").exists():       # Notion の登録は公開版に入っていない
        keys.append(("NOTION_TOKEN", "Notion の登録 publish（任意。無ければ MCP の手順）"))
    for key, why in keys:
        ok = bool(os.environ.get(key, "").strip())
        print(f"  {'○' if ok else '-'} {key}: {'設定あり' if ok else '無い'}（{why}）")

    # 解析の口はどれか 1 つあればよい（claude: docs/make_player.md の手順。codex: --driver codex）
    has_claude, has_codex = bool(shutil.which("claude")), bool(shutil.which("codex"))
    has_api = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    print(f"解析の口: Claude Code {'○' if has_claude else '-'} / Codex {'○' if has_codex else '-'} / API キー {'○' if has_api else '-'}")
    if not (has_claude or has_codex or has_api):
        print("  × 解析の口が無い。Claude Code を入れて自分のプランでログインする（README.md）")
        ng += 1
    if shell_api_key:
        print("  ! シェルの環境に ANTHROPIC_API_KEY がある。プランの枠で作るなら外す（残っていると従量課金になる）")

    work = ROOT / "work"
    n = sum(1 for d in work.iterdir() if (d / "bundle.json").exists()) if work.is_dir() else 0
    print(f"作った解説: {n} 本")

    port = int(os.environ.get("PFE_PORT", "8128"))
    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    print(f"ポート {port}: {'使用中（もう起動している？）' if busy else '空き'}")

    if ng:
        print(f"\n足りないものがある。部品の入れ方: {INSTALL}")
        return 1
    print("\nOK: 解説を作れる（docs/make_player.md）。一覧は py -3.13 scripts/serve.py")
    if not has_api:
        print(f"Web 版の「PDF を置く」には API キーが要る（{ENV_FILE} に `ANTHROPIC_API_KEY=...` の行で書く。チャットに貼らない）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
