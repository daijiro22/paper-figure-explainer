"""テストに使う実物の論文の場所。論文は配れないのでリポジトリには入れない。

置き場のフォルダを環境変数 PFE_TEST_PAPERS か tests/papers_root.txt（1 行目。Git の対象外）に書く。
どちらも無ければ、実物を使うテストはスキップされる。

Windows の表記（ドライブ名つき）は、WSL などの Linux では /mnt/<ドライブ>/ に読み替える
（アプリ制御ポリシーで Windows 版 pymupdf の DLL が止まる PC では、テストを WSL で回すため）。
"""
import os
import re
from pathlib import Path

_ROOT_FILE = Path(__file__).with_name("papers_root.txt")
_NO_PAPERS = "__no_test_papers__"       # 置き場が無いときの存在しないフォルダ（glob も exists も空振りしてスキップになる）


def _for_this_os(path: str) -> str:
    """Windows ではそのまま返す。Linux ではドライブ名つきの表記を /mnt/<ドライブ>/ に読み替える。"""
    if os.name == "nt":
        return path
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not m:
        return path
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def papers_root() -> str:
    """実物の論文の置き場。設定が無ければ存在しないフォルダの名前を返す。"""
    root = os.environ.get("PFE_TEST_PAPERS", "").strip()
    if not root and _ROOT_FILE.is_file():
        lines = _ROOT_FILE.read_text(encoding="utf-8").splitlines()
        root = lines[0].strip() if lines else ""
    return root or _NO_PAPERS


def local(rel: str) -> str:
    """置き場の中の相対パス（区切りは \\ でも / でもよい）を、今の OS で開ける形にする。"""
    return os.path.join(_for_this_os(papers_root()), *re.split(r"[\\/]", rel))
