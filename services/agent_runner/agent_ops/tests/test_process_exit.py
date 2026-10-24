# meiva:note
# The ops daemon's process leaves on its own terms: past a wedged arm, with the right code.

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_CHILD = Path(__file__).with_name("child_daemon_exit.py")


# meiva:note
# The empirical one, and the only shape that can catch this.
#
# `shutdown(wait=False)` looks like it releases the daemon and does not:
# `concurrent.futures.thread._python_exit` — registered via
# `threading._register_atexit` — joins every worker still in `_threads_queues` with
# NO bound, and `wait=False` does not remove a running thread from that mapping;
# 3.9+ also forces those workers non-daemon, so there is no way around it from the
# executor side. Measured before this fix: own pool + wedged worker +
# `shutdown(wait=False)` + `sys.exit(0)` was still alive minutes later.
#
# Nothing in-process can assert that: the failure IS the interpreter refusing to
# stop. So this drives the boot owner's `op_thread_pool` / `shutdown_op_pool` /
# shared `hard_exit` in a real subprocess with a genuinely stuck arm, and asserts the
# process is gone. It fails by timing out against the pre-fix code.
def test_a_wedged_arm_does_not_hold_the_process_exit(tmp_path: Path) -> None:
    ready = tmp_path / "wedged"
    started = time.monotonic()
    # meiva:note
    # The command is a fixed argv of the interpreter and an in-repo script: no shell, no user input.
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), "wedged_arm", str(tmp_path), str(ready)],
        capture_output=True,
        text=True,
        # meiva:note
        # the pre-fix code never returns; the timeout IS the failure
        timeout=30,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert done.returncode == 0, done.stderr[-2000:]
    # meiva:note
    # Generous: the point is "seconds, not never", not a latency budget.
    assert elapsed < 20, f"exit took {elapsed:.1f}s with an arm still wedged"


# meiva:note
# `hard_exit` replaced a `raise` on the crash path, so the code a supervisor
# reads has to still distinguish a crash from a clean stop.
def test_the_exit_code_survives_the_hard_exit() -> None:
    # meiva:note
    # The command is a fixed argv of the interpreter and an in-repo script: no shell, no user input.
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), "hard_exit", "1"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 1
