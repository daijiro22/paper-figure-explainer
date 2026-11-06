"""古い走査 PDF の OCR の字間空き（"t h e", "w a l l t i s s u e"）を語に戻す。

根拠の照合は空白をつぶさないので、字間空きがあると引用が落ちる。詰めるだけだと "walltissue" に
なるので、ほかの論文の本文から作った語の頻度表で語の切れ目を推定する。

  python scripts/fix_spaced_ocr.py <pid>          → ドライラン（例を出す。書かない）
  python scripts/fix_spaced_ocr.py <pid> --write  → text.json・abstract.json を書き換える（元は .orig.json に控える）
"""
import collections
import json
import math
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = Path(__file__).resolve().parent.parent / "work"
RUN = re.compile(r"(?<![A-Za-z])[A-Za-z](?: [A-Za-z])+(?![A-Za-z])")   # 英字 1 文字が空白をはさんで続く所
MAXLEN = 24


def build_freq(skip: str) -> collections.Counter:
    """語の頻度表: ほかの論文の text.json の本文から数える。"""
    freq = collections.Counter()
    for tj in WORK.glob("*/text.json"):
        if tj.parent.name == skip:
            continue
        for p in json.loads(tj.read_text(encoding="utf-8")).get("paragraphs", []):
            freq.update(w.lower() for w in re.findall(r"[A-Za-z]{2,}", p.get("text", "")))
    freq.update({"a": 200000, "i": 300})   # 1 文字の語は数えていないので足す
    return freq


def main(pid: str, write: bool) -> None:
    freq = build_freq(pid)
    total = sum(freq.values())

    def cost(w):
        n = freq.get(w.lower(), 0)
        # 2 回以下は OCR の崩れた語のことがあるので信用しない
        return -math.log(n / total) if n >= 3 else 30.0 + 5.0 * len(w)

    def segment(s):
        best = [0.0] + [math.inf] * len(s)
        back = [0] * (len(s) + 1)
        for i in range(1, len(s) + 1):
            for j in range(max(0, i - MAXLEN), i):
                c = best[j] + cost(s[j:i])
                if c < best[i]:
                    best[i], back[i] = c, j
        out, i = [], len(s)
        while i > 0:
            out.append(s[back[i]:i])
            i = back[i]
        return " ".join(reversed(out))

    changed, examples = 0, []

    def clean(s):
        nonlocal changed
        n = RUN.sub(lambda m: segment(m.group(0).replace(" ", "")), s)
        if n != s:
            changed += 1
            if len(examples) < 8 and len(s) < 400:
                examples.append((s[:140], n[:140]))
        return n

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return clean(o) if isinstance(o, str) else o

    files = [WORK / pid / "text.json", WORK / pid / "abstract.json"]
    new = {f: walk(json.loads(f.read_text(encoding="utf-8"))) for f in files if f.exists()}
    print(f"語の頻度表: {len(freq)} 語 / 直した文字列: {changed}")
    for a, b in examples:
        print(" 前:", a)
        print(" 後:", b)
    if write:
        for f, v in new.items():
            shutil.copy2(f, f.with_suffix(".orig.json"))
            f.write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
        print("書いた:", len(new), "ファイル（元は *.orig.json）")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], "--write" in sys.argv)
