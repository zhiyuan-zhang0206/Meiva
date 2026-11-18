# meiva:note
# Test child entrypoint invoked via `sys.executable -I` with fixed mode names.

from __future__ import annotations

import os
import sys
import time


def main() -> None:
    mode = sys.argv[1]
    if mode == "noop":
        return
    if mode == "fork_sleeper":
        pid = os.fork()
        if pid == 0:
            os.close(1)
            time.sleep(60)
        else:
            sys.stdout.write(f"{pid}\n")
            sys.stdout.flush()
        return
    raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
