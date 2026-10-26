# meiva:note
# The operations that change this process itself: its timezone, its config-fetch marker and
# its home. Each is for a program's top level and nothing else calls them; they are the only
# code in `base.host.env` that writes `os.environ` or the process clock directly, so this module
# is the root of that kind. `dotenv_boot` works on the mapping it is given and never imports it.
# A later step can make each function take the mapping and let the program entries pass
# `os.environ`, which would leave this module no root of its own.

from __future__ import annotations

import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["apply_process_timezone", "enter_scratch_home", "skip_config_fetch"]


# meiva:note
# Apply an explicitly resolved authoritative timezone at process startup.
#
# The configuration owner validates raw input and decides whether it holds an
# authoritative value. None leaves the host clock untouched. An invalid name
# that bypassed that boundary also leaves it untouched, matching the existing
# startup operation. A valid value is exported for children and refreshes the local clock
# with ``time.tzset``.
#
# This operation never reads Settings, a field default or ``MEIVA_TIMEZONE``.
# Read-only environment attachments must not call it.
def apply_process_timezone(name: str | None) -> None:
    if name is None:
        return
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return
    os.environ["TZ"] = name
    time.tzset()


# meiva:note
# Keep this process from fetching cluster configuration from a gateway.
#
# For a tool that must keep the real home (it reads this machine's own state) but
# has no business asking a gateway for anything: the config boot then stays
# settings-lite and dials nothing. Call it before the tool's first application
# import, as a program only (see `enter_scratch_home`).
def skip_config_fetch() -> None:
    from base.host.env.bootstrap import CONFIG_FETCH_ENV, CONFIG_FETCH_SKIP

    os.environ[CONFIG_FETCH_ENV] = CONFIG_FETCH_SKIP


# meiva:note
# Point this process tree at a fresh temporary home, whatever its environment carries.
#
# For a script that imports application code but must not touch the host's
# cluster (the scripts git hooks launch): it sets `MEIVA_HOME` to a new
# private directory and `MEIVA_CONFIG_FETCH=skip`, so nothing in the tree
# resolves `~/.meiva`, reads another home's `.env` or dials a gateway. Call it
# before the tool's first application import, and only when the tool runs as a
# program (`if __name__ == "__main__":`): every descendant inherits the home,
# so a module that enters one while being imported replaces the importing
# process's own. The directory is removed at exit.
#
# A pytest process refuses: the session home is set before any project import
# and every later test reads it, so a tool imported at collection that entered
# a scratch home would silently redirect the whole worker.
def enter_scratch_home() -> Path:
    import atexit
    import shutil
    import sys
    import tempfile

    if "pytest" in sys.modules:
        raise RuntimeError(
            "enter_scratch_home() was called inside a pytest process: it would replace the "
            "session's MEIVA_HOME for every later test. A tool calls it only as a program, "
            'behind `if __name__ == "__main__":`; run the tool in a subprocess to test it.'
        )

    home = Path(tempfile.mkdtemp(prefix="meiva-scratch-home-"))
    os.environ["MEIVA_HOME"] = str(home)
    skip_config_fetch()
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home
