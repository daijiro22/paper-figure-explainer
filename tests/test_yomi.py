"""読み上げの読み替え（pfe/yomi.py）: 長い表記が先・英字の境目・二重に置き換えない・当たらない文は 1 文字も変えない。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pfe import tts, yomi  # noqa: E402


@pytest.fixture
def table(tmp_path):
    p = tmp_path / "yomi.tsv"
    p.write_text("# 説明の行\n"
                 "THF\tテトラヒドロフラン\t例\n"
                 "mol%\tモルパーセント\n"
                 "%\tパーセント\n"
                 "m/z\tエムオーバーゼット\n"
                 "°C\t=\t正しく読める\n"
                 "ア\tTHF\t置き換えた結果をもう一度置き換えないかを見る\n", encoding="utf-8")
    return p


def test_longest_first(table):
    assert yomi.to_speech("5 mol% と 3%", table) == "5 モルパーセント と 3パーセント"


def test_ascii_boundary(table):
    assert yomi.to_speech("THF と THFA と 2THF と（THF）", table) == "テトラヒドロフラン と THFA と 2THF と（テトラヒドロフラン）"


def test_no_second_pass(table):
    # 「ア」→「THF」に置き換えた結果が、さらに「テトラヒドロフラン」にはならない
    assert yomi.to_speech("ア", table) == "THF"


def test_ok_mark_is_not_replaced(table):
    assert yomi.to_speech("353 °C", table) == "353 °C"


def test_identity_when_nothing_matches(table):
    s = "図の横軸は温度です。"
    assert yomi.to_speech(s, table) == s


def test_empty_table_is_identity(tmp_path):
    p = tmp_path / "yomi.tsv"
    p.write_text("# 例だけ\n# THF\tテトラヒドロフラン\n", encoding="utf-8")
    assert yomi.to_speech("THF の m/z", p) == "THF の m/z"


def test_candidates_skip_checked_and_strip_numbers(table):
    says = ["353°C と 280°C で β-O-4 が切れる。", "β-O-4 の m/z を見る。", "20mg を THF に溶かす。"]
    keys = {c["key"]: c for c in yomi.candidates(says, table)}
    assert "°C" not in keys and "#°C" not in keys      # 「=」の語は数値つきでも候補に出ない
    assert "THF" not in keys and "m/z" not in keys     # 表で置き換えた語も出ない
    assert keys["β-O-#"]["count"] == 2 and keys["β-O-#"]["form"] == "β-O-4"
    assert "mg" in keys                                 # 20mg → mg
    assert keys["mg"]["example"] == "20mg を テトラヒドロフラン に溶かす。"     # 例文は声に渡す形


def test_number_placeholder(tmp_path):
    p = tmp_path / "yomi.tsv"
    p.write_text("{n} K\t{n}ケルビン\n{n}〜{n}\t{n}から{n}\n", encoding="utf-8")
    assert yomi.to_speech("毎分10 K で、K は札。", p) == "毎分10ケルビン で、K は札。"
    assert yomi.to_speech("1.5K と K2", p) == "1.5ケルビン と K2"
    assert yomi.to_speech("280〜380 °C", p) == "280から380 °C"
    assert yomi.to_speech("1〜9 と 12.5〜30", p) == "1から9 と 12.5から30"


def test_range_candidate_is_the_symbol(tmp_path):
    p = tmp_path / "yomi.tsv"
    p.write_text("", encoding="utf-8")
    keys = {c["key"] for c in yomi.candidates(["左端の1〜9は番号。280〜380 で減る。"], p)}
    assert "〜" in keys and not any(k.startswith("〜#") for k in keys)


def test_synth_hashes_spoken_text(tmp_path, monkeypatch, table):
    spoken = []

    async def fake_speak(text, out, voice, rate):
        spoken.append(text)
        Path(out).write_bytes(b"x")

    monkeypatch.setattr(tts, "_speak", fake_speak)
    monkeypatch.setattr(tts, "_duration", lambda p: 1.0)
    monkeypatch.setattr(tts, "to_speech", lambda t: yomi.to_speech(t, table))
    p, _ = tts.synth("THF に溶かす", tmp_path)
    assert spoken == ["テトラヒドロフラン に溶かす"]
    assert p.name == tts.audio_name("テトラヒドロフラン に溶かす")
    assert not list(tmp_path.glob("*.part"))           # 書き終えてから名前を付ける
    # 当たらない文の名前は今までと同じ（既存の声を作り直さない）
    p2, _ = tts.synth("図の横軸", tmp_path)
    import hashlib
    assert p2.name == hashlib.sha1(f"{tts.VOICE}|{tts.RATE}|図の横軸".encode("utf-8")).hexdigest()[:12] + ".mp3"
