from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

CHILD_MODULE = "scripts.hooks.tests.child_process"
LONG_SLEEP_SECONDS = 120.0


def main(argv: Sequence[str]) -> int:
    mode = argv[0]
    if mode == "exit":
        return int(argv[1])
    if mode == "sleep":
        time.sleep(LONG_SLEEP_SECONDS)
        return 0
    child = subprocess.Popen([sys.executable, "-m", CHILD_MODULE, "sleep"])  # noqa: S603
    Path(argv[1]).write_text(str(child.pid))
    time.sleep(LONG_SLEEP_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
