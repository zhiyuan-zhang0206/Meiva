# meiva:note
# `python -m scripts.host_ops.worktree_gc` — the guard reads argv once and hands it to `main`.

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
