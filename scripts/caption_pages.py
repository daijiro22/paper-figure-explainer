"""図表のキャプションらしい行があるページを出す（枠を手で決める前に、見るページを絞るため）。

  python scripts/caption_pages.py <pid>
      → p5: Figure 1: Demonstration of ... | 画像 1 個

行頭の「Fig. 1」「Figure 1」「TABLE I」「Table 2」を拾う。本文の「Figure 2 shows」も混ざるので、最後は目で見る。
LLM は使わない。読むだけで何も書かない。
"""
import re
import sys
from pathlib import Path

import pymupdf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = Path(__file__).resolve().parent.parent / "work"
# TABLE と番号が別の行に分かれる古い組版（Schuerch 1952）も拾う
CAP = re.compile(r"(?m)^\s*(?:Fig\.?|Figure|FIG\.?|FIGURE|Table|TABLE)\s*[0-9IVXl]+[^\n]{0,70}")


def main(pid: str) -> int:
    doc = pymupdf.open(WORK / pid / "source.pdf")
    for i, page in enumerate(doc, start=1):
        hits = [" ".join(h.split())[:80] for h in CAP.findall(page.get_text())]
        if hits:
            print(f"p{i}: {' / '.join(hits[:6])} | 画像 {len(page.get_images())} 個")
    print(f"全 {len(doc)} ページ")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
