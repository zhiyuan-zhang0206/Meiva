from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    gated = sys.argv[2] == "gated"
    os.close(sys.stdout.fileno())
    (root / "ready").touch()
    deadline = time.monotonic() + 10
    stdin_closed = False
    while time.monotonic() < deadline:
        if (root / "release").exists():
            return 3
        if not stdin_closed and select.select([sys.stdin], [], [], 0.01)[0]:
            if not os.read(sys.stdin.fileno(), 1):
                stdin_closed = True
                (root / "stdin-closed").touch()
                if not gated:
                    return 3
        elif stdin_closed:
            select.select([], [], [], 0.01)
    raise TimeoutError("the owned EOF child was not released")


if __name__ == "__main__":
    raise SystemExit(main())
