from __future__ import annotations

import os
from pathlib import Path

from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon


# meiva:note
# A pidfile that names a live process of another module is stale, not a running
# daemon: this test process is alive, but its argv names another module.
def test_daemon_guard_rejects_a_live_pid_that_is_not_this_daemon(tmp_path: Path) -> None:
    pidfile = tmp_path / "task_maintenance.pid"
    pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")

    assert daemon._is_running(pidfile=pidfile) is False
