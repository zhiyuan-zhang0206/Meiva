# meiva:note
# What the schedule manager does to one schedule's session: facts in, an action out.
#
# The manager's shell reads the durable desired revision (``Desired``), whether the session is
# live and which revision it was launched for, and executes the action these functions return:
# reap, adopt, hold, launch. Lost or ambiguous starts are errors requiring explicit start/restart;
# a revision whose launch was admitted is never launched again.

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from enum import StrEnum
from typing import NamedTuple

__all__ = [
    "Desired",
    "Prepare",
    "Reclaim",
    "SyncAction",
    "launch_command",
    "merge_launch_env",
    "plan_sync",
    "prepare_verdict",
    "reclaim_verdict",
    "revision_in",
]

_REVISION = re.compile(r"-m services\.wake\.schedule_manager\.runner \d+ (\d+);")


# meiva:note
# The durable desired state of one schedule, as its row says.
class Desired(NamedTuple):
    enabled: bool
    status: str
    revision: int
    applied: int
    launch_generation: str | None
    secret_env_vars: tuple[str, ...] = ()


class SyncAction(StrEnum):
    # meiva:note
    # deleted or disabled: its session must go
    REAP_UNWANTED = "reap_unwanted"
    # meiva:note
    # a revision-zero session whose provenance is gone: keep
    ADOPT_LEGACY = "adopt_legacy"
    # meiva:note
    # live, provenance unreadable, not legacy: blocked
    HOLD_UNREADABLE = "hold_unreadable"
    # meiva:note
    # live and launched for the wanted revision
    ADOPT_CURRENT = "adopt_current"
    # meiva:note
    # live for another revision: reap, then launch
    REPLACE_STALE = "replace_stale"
    # meiva:note
    # gone, already completed or errored at this revision
    SETTLED = "settled"
    # meiva:note
    # gone while still marked running: lost, or host replaced
    RECLAIM_RUNNING = "reclaim_running"
    LAUNCH = "launch"


# meiva:note
# The action that converges one schedule to its durable desired revision.
#
# ``observed`` is the revision the live session was launched for; None when the session is
# absent or its provenance could not be read.
def plan_sync(wanted: Desired | None, *, live: bool, observed: int | None) -> SyncAction:
    if wanted is None or not wanted.enabled:
        return SyncAction.REAP_UNWANTED
    if live:
        if observed is None:
            # meiva:note
            # A legacy session at revision zero is adopted. For a pending versioned command,
            # missing provenance means uncertain, not permission to kill a possibly
            # already-applied execution.
            if wanted.revision == wanted.applied == 0:
                return SyncAction.ADOPT_LEGACY
            return SyncAction.HOLD_UNREADABLE
        if observed == wanted.revision:
            return SyncAction.ADOPT_CURRENT
        return SyncAction.REPLACE_STALE
    if wanted.revision == wanted.applied and wanted.status in ("running", "completed", "error"):
        return SyncAction.RECLAIM_RUNNING if wanted.status == "running" else SyncAction.SETTLED
    return SyncAction.LAUNCH


class Prepare(StrEnum):
    # meiva:note
    # nothing stands in the way of the launch
    CLEAR = "clear"
    # meiva:note
    # the session already runs the wanted revision: do not launch
    ADOPT = "adopt"
    # meiva:note
    # uncertain: no authority to replace it
    HOLD = "hold"
    # meiva:note
    # a stale survivor: reap it first
    REAP = "reap"


# meiva:note
# What a same-name session means for a launch about to happen.
#
# ``current_generation`` is whether the survivor belongs to this allocation generation; only
# then is its provenance consulted.
def prepare_verdict(
    wanted: Desired, *, present: bool, current_generation: bool, observed: int | None
) -> Prepare:
    if not present:
        return Prepare.CLEAR
    if current_generation:
        if observed == wanted.revision:
            return Prepare.ADOPT
        if observed is None:
            if wanted.revision > 0:
                # meiva:note
                # uncertain allocation
                return Prepare.HOLD
            if wanted.applied == 0 and wanted.status == "running":
                # meiva:note
                # legacy healthy execution observed after a stale scan
                return Prepare.HOLD
    return Prepare.REAP


class Reclaim(StrEnum):
    # meiva:note
    # the session host is the one the launch happened on
    NOT_REPLACED = "not_replaced"
    # meiva:note
    # the window's relaunch budget is spent
    SUPPRESSED = "suppressed"
    # meiva:note
    # one automatic relaunch in the new host generation
    AUTHORIZE = "authorize"


# meiva:note
# Whether a running schedule whose session is gone may be relaunched after its session host
# was replaced. A replay of the intent already in flight is always allowed.
def reclaim_verdict(
    *,
    generation: str | None,
    launched_in: str | None,
    replay: bool,
    attempts: int,
    limit: int,
) -> Reclaim:
    if generation is None or launched_in is None or launched_in == generation:
        return Reclaim.NOT_REPLACED
    if not replay and attempts >= limit:
        return Reclaim.SUPPRESSED
    return Reclaim.AUTHORIZE


# meiva:note
# The shell command a schedule's session runs.
#
# ``cd`` first: the login shell's own profile can move the cwd the daemon forked with (macOS
# path_helper rebuilds PATH; a profile ``cd`` would strand the relative runner path). The
# ``; exit $?`` tail makes the shell end with the runner, so a dead runner never leaves a live
# session behind for the reconcile's session-existence liveness to misread.
def launch_command(repo_root: str, schedule_id: int, revision: int) -> str:
    return (
        f"cd {shlex.quote(repo_root)} && "
        f".venv/bin/python -m services.wake.schedule_manager.runner {schedule_id} {revision}; exit $?"
    )


# meiva:note
# The revision a session was launched for, read from the command it started with.
#
# None when the command is unknown (missing evidence never permits replacement). A legacy
# command, which carries no revision, belongs to revision zero.
def revision_in(initial_command: str | None) -> int | None:
    if initial_command is None:
        return None
    match = _REVISION.search(initial_command)
    return 0 if match is None else int(match.group(1))


def merge_launch_env(
    base_env: Mapping[str, str],
    *,
    schedule_id: int,
    timezone: str,
    secret_names: tuple[str, ...],
    resolved_secrets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    del schedule_id
    env = dict(base_env)
    env["MEIVA_TIMEZONE"] = timezone
    if secret_names and resolved_secrets:
        for k, v in resolved_secrets.items():
            if k in secret_names and not k.startswith("MEIVA_"):
                env[k] = v
    return env
