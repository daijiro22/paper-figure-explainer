"""概要（fig/overview.analysis.json）を検査する。run.py は概要の検査結果を出さないので、手で書いたあとに使う。

    py -3.13 scripts/check_overview.py <paper_id>

指摘が無ければ終了コード 0、あれば 1。LLM は使わない。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pfe.overview import check


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    work = ROOT / "work" / sys.argv[1]
    # 検査に使う 3 つを読む: 書いた概要・アブストラクト・この論文の図表の id
    answer = json.loads((work / "fig" / "overview.analysis.json").read_text(encoding="utf-8"))
    abstract = json.loads((work / "abstract.json").read_text(encoding="utf-8"))["text"]
    figures = json.loads((work / "figures.json").read_text(encoding="utf-8"))
    figures = figures if isinstance(figures, list) else figures["figures"]
    errors = check(answer, abstract, [f["id"] for f in figures])
    for e in errors:
        print(f"- {e}")
    print(f"検査の指摘 {len(errors)} 件")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
