import sys
from pathlib import Path

from base.log import logger
from base.native_process.root_control.instance_lock import (
    AlreadyRunningError,
    acquire_instance_lock,
    release_instance_lock,
)
from base.paths import run_dir

_INSTANCE_LOCK = "agent_host.lock"

__all__ = ["host_serving", "release_instance_lock", "take_instance_lock"]


# meiva:note
# One agent host serves a home. The lock on `<home>/run/agent_host.lock` lasts as long as
# the host and its fd is never inherited by an exec child. A second host exits at once.
def take_instance_lock(home: Path) -> int:
    try:
        return acquire_instance_lock(run_dir(home), _INSTANCE_LOCK)
    except AlreadyRunningError as exc:
        logger.error(
            "[agent-host] {error}; another agent host is serving this home, exiting",
            error=str(exc),
        )
        sys.exit(1)


# meiva:note
# Whether an agent host serves `home` right now: its instance lock is held. A free lock is
# taken and released at once, so asking never delays the host that starts next.
def host_serving(home: Path) -> bool:
    try:
        fd = acquire_instance_lock(run_dir(home), _INSTANCE_LOCK)
    except AlreadyRunningError:
        return True
    release_instance_lock(fd)
    return False
