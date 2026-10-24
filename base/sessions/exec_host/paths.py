# meiva:note
# Locations and bounds shared by the exec-host service and its clients.
#
# Everything lives under `$MEIVA_HOME/run/`: the service's unix socket (`exec-host.sock`)
# and its instance lock (`exec-host.lock`). A deeply nested `$MEIVA_HOME` that exceeds the
# platform `sun_path` bound falls back to `/tmp/meiva-exec-<uid>/<digest>.sock` so the
# service and its clients compute the exact same path.

from __future__ import annotations

import hashlib
import os
from pathlib import Path

SUN_PATH_MAX = 100

SERVICE_UNIT = "exec-host"


# meiva:note
# The exec-host service's unix socket in the run directory `run`; `<home>/run/exec-host.sock`.
def service_socket_path(run: Path) -> Path:
    natural = run / "exec-host.sock"
    if len(str(natural)) <= SUN_PATH_MAX:
        return natural
    digest = hashlib.sha1(f"{run}\0exec-host".encode(), usedforsecurity=False).hexdigest()
    return fallback_dir() / f"{digest[:12]}.sock"


# meiva:note
# Where a socket too long for its home lives: `/tmp/meiva-exec-<uid>` (owner-only).
def fallback_dir() -> Path:
    return Path("/tmp") / f"meiva-exec-{os.getuid()}"  # noqa: S108


# meiva:note
# The service's instance lock (`run/exec-host.lock`), held while it runs.
def lock_path(run: Path) -> Path:
    return run / "exec-host.lock"
