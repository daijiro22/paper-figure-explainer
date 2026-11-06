"""Web 版を起動する（http://127.0.0.1:8128/）。

  py -3.13 scripts/serve.py     （Windows で pymupdf が動かない PC は、WSL の venv の python で同じファイルを動かす）
依存: fastapi uvicorn（mutagen は ffprobe が無い環境で音声の長さを測るのに使う）
環境変数: PFE_DB（一覧の DB の場所）・PFE_DRIVER（codex|claude|manual）・PFE_PORT
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import uvicorn  # noqa: E402

from web.app import create_app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PFE_PORT", "8128"))
    print(f"http://127.0.0.1:{port}/")
    # 127.0.0.1 だけ: 論文の図は著作物なので、ほかの機械から見えるようにしない
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
