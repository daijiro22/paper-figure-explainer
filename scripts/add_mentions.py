"""text.json の fig_mentions に段落を足す（検査が引用を認める範囲を広げるだけ。本文は変えない）。

本文の誤認識（"Figure l"）や「Figure 1 の言及が別の文にある」段落は、図の説明なのに検査で引用を断られる。

  python scripts/add_mentions.py <pid> fig1=p15,p16 fig2=p20          → 段落の本文を出すだけ（その図の説明か目で見る）
  python scripts/add_mentions.py <pid> fig1=p15,p16 fig2=p20 --write  → 足す
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = Path(__file__).resolve().parent.parent / "work"


def main(pid, specs, write):
    path = WORK / pid / "text.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    paras = {p["id"]: p for p in d["paragraphs"]}
    added = 0
    for spec in specs:
        fig, ids = spec.split("=")
        cur = d["fig_mentions"].setdefault(fig, [])
        have = {m["para"] for m in cur}
        for i in ids.split(","):
            print(f"{fig} {i} p{paras[i]['page']}: {paras[i]['text'][:360]}\n")
            if i not in have:
                cur.append({"para": i, "sentence": 0})
                added += 1
    print("足す数:", added)
    if write:
        # 解析中の Agent が読んでいても壊れないよう、一時ファイルから置き換える
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        print("書いた:", path)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    if len(args) < 2:
        print(__doc__)
        sys.exit(2)
    main(args[0], args[1:], "--write" in sys.argv)
