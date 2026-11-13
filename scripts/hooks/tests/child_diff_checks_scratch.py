# meiva:note
# Synthetic short-process fixture for run-owner interruption tests.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import psutil
from pydantic import BaseModel

from scripts.hooks.diff_checks_pytest_child import (
    PytestChildUnreapedError,
    PytestInterrupted,
    run_pytest_child,
)
from scripts.hooks.diff_checks_scratch import pytest_scratch


class DescendantAnchor(BaseModel):
    pid: int
    birth: float
    group: int
    session: int
    leader: int
    argv: list[str]


def main(arguments: list[str]) -> int:
    mode, *values = arguments
    if mode == "exit":
        print("diagnostic")
        return int(values[0])
    if mode == "sleep":
        _ = Path(values[0]).write_text(str(os.getpid()))
        time.sleep(30)
        return 0
    if mode == "cleanup-interrupt":
        parent_text, run_text, kind = values
        if kind not in ("TERM", "INT"):
            raise ValueError(f"unknown interruption {kind}")

        def fail_start(
            argv: Sequence[str], root: Path, environment: Mapping[str, str]
        ) -> subprocess.Popen[str]:
            del argv, root, environment
            if kind == "TERM":
                os.kill(os.getpid(), signal.SIGTERM)
                raise OSError("synthetic constructor failure")
            raise KeyboardInterrupt

        def unknown_stop(child: subprocess.Popen[str] | None) -> None:
            assert child is None
            raise PytestChildUnreapedError("synthetic group observation unknown")

        try:
            with pytest_scratch(Path(parent_text)) as scratch:
                _ = Path(run_text).write_text(str(scratch))
                _ = run_pytest_child(
                    (sys.executable,),
                    Path.cwd(),
                    environment={},
                    start=fail_start,
                    stop=unknown_stop,
                )
        except PytestInterrupted as interruption:
            signal.raise_signal(interruption.signum)
            raise SystemExit(128 + interruption.signum) from None
        return 0
    if mode == "leader":
        record = Path(values[0])
        child_argv = [
            sys.executable,
            "-m",
            "scripts.hooks.tests.child_diff_checks_scratch",
            "sleep",
            values[1],
        ]
        # meiva:note
        # An anchored same-session descendant closes inherited output before leader exit.
        child = subprocess.Popen(child_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        process = psutil.Process(child.pid)
        anchor = DescendantAnchor(
            pid=child.pid,
            birth=process.create_time(),
            group=os.getpgid(child.pid),
            session=os.getsid(child.pid),
            leader=os.getpid(),
            argv=child_argv,
        )
        try:
            _ = record.write_text(anchor.model_dump_json())
        except BaseException:
            child.terminate()
            _ = child.wait(timeout=5)
            raise
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
