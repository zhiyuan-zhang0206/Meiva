# meiva:note
# Synthetic short-process fixture for run-owner interruption tests.

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from scripts.hooks.diff_checks_pytest_child import PytestInterrupted, run_pytest_child
from scripts.hooks.diff_checks_scratch import pytest_scratch


def main(arguments: list[str]) -> int:
    mode, *values = arguments
    if mode == "exit":
        print("diagnostic")
        return int(values[0])
    if mode == "sleep":
        _ = Path(values[0]).write_text(str(os.getpid()))
        time.sleep(30)
        return 0
    if mode == "owner":
        parent, pid_file, run_file = map(Path, values)
        try:
            with pytest_scratch(parent) as scratch:
                _ = run_file.write_text(str(scratch))
                run_pytest_child(
                    (
                        sys.executable,
                        "-m",
                        "scripts.hooks.tests.child_diff_checks_scratch",
                        "sleep",
                        str(pid_file),
                    ),
                    Path.cwd(),
                    environment=dict(os.environ),
                )
        except PytestInterrupted as interruption:
            signal.raise_signal(interruption.signum)
            raise SystemExit(128 + interruption.signum) from None
        return 0
    raise ValueError(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
