from __future__ import annotations

import sys
from pathlib import Path

from meizel.graph.model import MeizelError, Sources
from meizel.graph.universe import GitRepo, Universe
from meizel.language.scan import scan_source


def main() -> int:
    path = sys.argv[1]
    source = b"answer = 1" if path.endswith(".py") else b"const answer = 1;"
    try:
        scan_source(Universe(GitRepo(Path(sys.argv[2])), [path]), path, source, Sources())
    except MeizelError as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
