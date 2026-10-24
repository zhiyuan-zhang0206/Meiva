# meiva:note
# Native survivor diagnostics for shell-session stop and maintenance waits.
#
# Reports retain the captured birth even when live facts cannot be read. The
# shell-session boundary raises StopIncompleteError; its lifecycle journal stores the
# same structured inventory.

from __future__ import annotations

import os
from collections.abc import Iterable

import psutil

from base.native_process.kernel_identity import pid_starttime_ticks
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane.maintenance.survivors import StopSurvivor, SurvivorRole


# meiva:note
# A stop's deadline expired with owned processes still alive.
#
# The message carries the operator-readable inventory; `survivors` carries
# the same entries as JSON-safe dicts for the durable journal, and `stage`
# names the stop phase that hit the deadline (issue #2162).
class StopIncompleteError(TimeoutError):
    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        survivors: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.survivors: list[dict[str, object]] = list(survivors or [])


# meiva:note
# Captured identities still present, in pid order.
#
# Reporting must not drop a survivor it cannot verify: an identity whose birth cannot be re-read
# counts as present (it is then listed without live facts), while a confirmed birth mismatch
# counts as gone, the same answer signalling gives. An unverifiable birth (the RuntimeError of
# `OwnedProcess.live`) or an unreadable process (`psutil.AccessDenied`) keeps the survivor
# listed rather than dropping it.
def live_identities(identities: Iterable[OwnedProcess]) -> list[OwnedProcess]:
    present: list[OwnedProcess] = []
    for identity in identities:
        try:
            alive = identity.live()
        except (RuntimeError, psutil.Error):
            alive = True
        if alive:
            present.append(identity)
    return sorted(present, key=lambda identity: identity.pid)


# meiva:note
# Whether `identity` is still the process it was captured as.
#
# Guards the report's live reads exactly as signal delivery is guarded: a PID recycled since
# capture must not describe itself with a stranger's cmdline. An unconfirmed birth (the platform
# cannot re-read it) reads as not matched; the entry then keeps the captured identity and no
# live facts.
def identity_matches(identity: OwnedProcess) -> bool:
    try:
        if identity.starttime is not None:
            return pid_starttime_ticks(identity.pid) == identity.starttime
        process = psutil.Process(identity.pid)
        return identity.birth_matches(process) and process.status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except (psutil.Error, OSError, RuntimeError):
        return False


# meiva:note
# Read one survivor's live facts; reads only and never signals. An unreadable process is reported
# as that, never dropped.
#
# `role` is the closure's wire string; a role outside the shell/job vocabulary raises
# `ValueError` before any live fact is read, because it means the service and this stop disagree
# on the contract.
def capture_survivor(
    identity: OwnedProcess, *, service: str | None, role: str, pgid_hint: int | None = None
) -> StopSurvivor:
    survivor_role = SurvivorRole(role)
    ppid: int | None = None
    pgid = pgid_hint
    status: str | None = None
    cmdline: str | None = None
    if identity_matches(identity):
        try:
            process = psutil.Process(identity.pid)
            ppid = process.ppid()
            status = process.status()
            cmdline = " ".join(process.cmdline()) or None
        except (psutil.Error, OSError):
            ppid = status = cmdline = None
        if pgid is None:
            try:
                pgid = os.getpgid(identity.pid)
            except OSError:
                pgid = None
    return StopSurvivor(
        pid=identity.pid,
        role=survivor_role,
        service=service,
        ppid=ppid,
        pgid=pgid,
        status=status,
        birth=identity.birth,
        starttime=identity.starttime,
        cmdline=cmdline,
    )
