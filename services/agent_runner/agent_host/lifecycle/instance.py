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

__all__ = ["release_instance_lock", "take_instance_lock"]


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
