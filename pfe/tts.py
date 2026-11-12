"""ナレーション音声: ステップの say を edge-tts で mp3 にし、長さ（秒）を測る。

声はいま edge-tts（声の決めごとは CLAUDE.md の Do NOT）。「文字列 → mp3 と長さ」の 1 関数に閉じる。同じ文・同じ声なら作り直さない（ファイル名に文の hash）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from pfe.yomi import to_speech

VOICE = "ja-JP-KeitaNeural"   # 男性の声（ja-JP の男性は Keita だけ。施主 2026-10-29「男性で1.3倍」）
RATE = "+30%"                 # 1.3 倍速


def _duration(path: Path) -> float | None:
    """ffprobe で音声の長さ（秒）を測る。ffprobe が無いときは mutagen で測る。
    長さが None だと player が文字数から長さを見積もり、声と画面がずれる（2026-10-29）。"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        return round(float(json.loads(r.stdout)["format"]["duration"]), 3)
    except Exception:
        pass
    try:
        from mutagen.mp3 import MP3
        return round(MP3(str(path)).info.length, 3)
    except Exception:
        return None


async def _speak(text: str, out: Path, voice: str, rate: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(str(out))


def audio_name(spoken: str, voice: str = VOICE, rate: str = RATE) -> str:
    """声に渡す文から mp3 の名前を作る。同じ文・同じ声なら同じ名前になり、作り直さない。"""
    return hashlib.sha1(f"{voice}|{rate}|{spoken}".encode("utf-8")).hexdigest()[:12] + ".mp3"


def synth(text: str, audio_dir: Path, voice: str = VOICE, rate: str = RATE,
          *, apply_yomi: bool = True) -> tuple[Path, float | None]:
    """text を mp3 にして (パス, 秒) を返す。作成済みなら再利用する。

    読み間違える語は pfe/yomi.tsv で読みに置き換えてから声にする（字幕は変えない。施主 2026-11-08）。
    apply_yomi=False は、すでに置き換えた文を渡すとき（読みの確認シート）。"""
    spoken = to_speech(text) if apply_yomi else text
    audio_dir.mkdir(parents=True, exist_ok=True)
    out = audio_dir / audio_name(spoken, voice, rate)
    if not out.exists() or out.stat().st_size == 0:
        part = out.with_suffix(".part")     # 途中で止まった書きかけを完成品として再利用しないよう、書き終えてから名前を付ける
        asyncio.run(_speak(spoken, part, voice, rate))
        part.replace(out)
    return out, _duration(out)
