"""Exec-domain session probes for the proc tests; never imported by a production entry.

Run as ``python _hosting_child.py <scenario> ...`` inside a new session:

- ``predicate`` prints ``repr(hosting_exec_domain())``;
- ``reparent <out-file> <entry>`` starts a nohup child through ``sh -c`` that reparents out of this
  process's lineage, then stays alive while the child records its reading in ``<out-file>``;
- ``reparented_probe <leader-pid>`` is that child's body.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from base.host.proc import hosting_exec_domain


def predicate() -> None:
    sys.stdout.write(f"{hosting_exec_domain()!r}\n")


def reparent() -> None:
    sys.stdout.write(f"{os.getpid()}\n")
    sys.stdout.flush()
    command = (
        "nohup "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(__file__)
        + " reparented_probe "
        + shlex.quote(str(os.getpid()))
        + " > "
        + shlex.quote(sys.argv[2])
        + " 2>&1 &"
    )
    subprocess.run(["sh", "-c", command], check=False)  # noqa: S603
    # Stay alive while the reparented child takes its reading: a live exec
    # root is exactly the state the membership probe must see (the domain
    # lives until the call's turn ends).
    time.sleep(5.0)


def reparented_probe() -> None:
    import psutil

    time.sleep(1.0)
    ancestors = {p.pid for p in psutil.Process().parents()}
    sys.stdout.write(f"{hosting_exec_domain()} {os.getsid(0)} {int(sys.argv[2]) in ancestors}\n")


def sleep() -> None:
    time.sleep(300.0)


def noop() -> None:
    return


def exit_with() -> None:
    sys.stdout.write(f"{sys.argv[2]}\n")
    sys.stdout.flush()
    sys.exit(int(sys.argv[3]))


def parent_long() -> None:
    child = subprocess.Popen([sys.executable, "base/host/tests/_hosting_child.py", "sleep"])
    Path(sys.argv[2]).write_text(str(child.pid))
    time.sleep(300.0)


def parent_short() -> None:
    child = subprocess.Popen([sys.executable, "base/host/tests/_hosting_child.py", "noop"])
    Path(sys.argv[2]).write_text(str(child.pid))
    time.sleep(300.0)


def _touch_term(_signum: int, _frame: object) -> None:
    Path(sys.argv[3]).touch()


def ignore_term() -> None:
    signal.signal(signal.SIGTERM, _touch_term)
    Path(sys.argv[2]).touch()
    time.sleep(30.0)


SCENARIOS = {
    "predicate": predicate,
    "reparent": reparent,
    "reparented_probe": reparented_probe,
    "sleep": sleep,
    "noop": noop,
    "exit_with": exit_with,
    "parent_long": parent_long,
    "parent_short": parent_short,
    "ignore_term": ignore_term,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
