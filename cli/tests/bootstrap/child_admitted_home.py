# meiva:note
# `admitted_home` as a start process runs it: a process that inherited another home and that
# home's database login enters the home named on the command line, holds its lifecycle lock and
# sees nothing of the other home.
#
# It runs as a child so that entering a home, which rewrites the process's environment, is the
# child's own effect: the test hands it the inherited environment as the child's `env` and never
# writes its own.

import os
import sys
from pathlib import Path

from base.host.db_authority.delivery import GENERATION_ENV
from base.native_process.os_platform import LockTimeoutError, file_lock
from cli.start_intent import admitted_home

home = Path(sys.argv[1])
sidecar = home / "start-intent.lock.holder.json"
assert os.environ["MEIVA_HOME"] != str(home), "the child must start in another home"
assert "MEIVA_DB_URL" in os.environ, "the child must inherit another home's login"

with admitted_home(home, pid=43210, started_at="2026-11-04T03:00:00+00:00"):
    assert os.environ["MEIVA_HOME"] == str(home)
    assert "MEIVA_DB_URL" not in os.environ, "another home's login is not this home's"
    assert GENERATION_ENV not in os.environ, "the foreign login and generation leave together"
    assert sidecar.is_file()
    try:
        with admitted_home(home, timeout_s=0.05):
            raise AssertionError("another lifecycle operation entered during start")
    except LockTimeoutError as exc:
        message = str(exc)
        assert "active holder: pid=43210" in message
        assert "purpose='cli.start_intent'" in message
        assert "since=2026-11-04T03:00:00+00:00" in message

assert not sidecar.exists()
with file_lock(home / "start-intent.lock", timeout_s=0.05):
    pass
