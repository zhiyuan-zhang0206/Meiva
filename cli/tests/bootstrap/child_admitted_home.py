# meiva:note
# `admitted_home` as a start process runs it: a process that inherited another home and that
# home's database login enters the home named on the command line, holds its lifecycle lock and
# sees nothing of the other home.
#
# It runs as a child so that entering a home, which rewrites the process's environment, is the
# child's own effect: the test hands it the inherited environment as the child's `env` and never
# writes its own.

import contextlib
import os
import sys
from pathlib import Path

from base.native_process.os_platform import LockTimeoutError, file_lock
from cli.start_intent import admitted_home

home = Path(sys.argv[1])
assert os.environ["MEIVA_HOME"] != str(home), "the child must start in another home"
assert "MEIVA_DB_URL" in os.environ, "the child must inherit another home's login"

with admitted_home(home):
    assert os.environ["MEIVA_HOME"] == str(home)
    assert "MEIVA_DB_URL" not in os.environ, "another home's login is not this home's"
    with (
        contextlib.suppress(LockTimeoutError),
        file_lock(home / "start-intent.lock", timeout_s=0.05),
    ):
        raise AssertionError("another lifecycle operation entered during start")

with file_lock(home / "start-intent.lock", timeout_s=0.05):
    pass
