# meiva:note
# Read a host's pause posture.
#
# The `host_deploy_state.posture` row is a status projection: no lifecycle command
# writes it any more, and nothing gates on it.

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

import base.deploy.state.host_deploy_state
from base.db import Database

_log = logging.getLogger(__name__)
_UNSET = object()


# meiva:note
# Whether this host is paused — the `host_deploy_state.posture` row written
# by the gateway's pause fan-out (R1, Task #1021).
#
# Status readers consume this database projection. A projection read failure
# (DB unreachable) reads as NOT paused — the same conservative direction the
# old file stat had (an unreadable flag was an absent flag).
def is_paused(
    db: Database,
    state: base.deploy.state.host_deploy_state.HostDeployState | object | None = _UNSET,
    *,
    machine_name: Callable[[], str],
) -> bool:
    if state is _UNSET:
        try:
            resolved_state = base.deploy.state.host_deploy_state.read(db, machine_name())
        except Exception:
            _log.warning(
                "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
                exc_info=True,
            )
            return False
    else:
        resolved_state = cast(base.deploy.state.host_deploy_state.HostDeployState | None, state)
    return resolved_state is not None and resolved_state.posture == "paused"
