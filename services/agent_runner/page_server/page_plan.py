# meiva:note
# What one page-server pass decides, as functions over values.
#
# `daemon.reconcile_rows` owns the effects (database reads and writes, the pty service, the
# health probe). Every decision between two effects lives here, so a test passes the rows,
# handles and session names it wants and asserts the returned verdict:
#
# - the shell command that starts a row's page server,
# - which supervised handles no longer match their row,
# - which persisted sessions of closed rows are still alive and must be killed,
# - whether a row is adopted, created, or closed as unavailable,
# - whether a handle is old enough for its server to be probed,
# - which live page shell each shell pid belongs to.

from __future__ import annotations

import shlex
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Admission",
    "PageKey",
    "PageRow",
    "ServerHandle",
    "admission",
    "health_due",
    "server_command",
    "sessions_to_close",
    "shell_pids",
    "stale_handles",
]

PAGE_SERVER_MODULE = "services.agent_runner.page_server.server"

# meiva:note
# A new pty session needs a short window to finish its interactive-shell startup and receive its
# initial command before a health probe can make a decision.
SPAWN_VERIFY_TIMEOUT_S = 5.0

PageKey = tuple[int, str]


# meiva:note
# One open serve row of this host, as the pass read it.
@dataclass(frozen=True)
class PageRow:
    id: int
    agent_id: int
    name: str
    port: int
    host: str
    serve_dir: str
    server_token: str | None = field(default=None, repr=False)
    session_name: str | None = None


# meiva:note
# One page-server command supervised through its persistent shell.
@dataclass(frozen=True)
class ServerHandle:
    agent_id: int
    name: str
    port: int
    serve_dir: str
    token: str = field(repr=False)
    session_name: str
    last_launch_monotonic: float


# meiva:note
# What a pass does with an open row it has no handle for.
class Admission(Enum):
    ADOPT = "adopt"
    CREATE = "create"
    SESSION_LOST = "session-lost"
    DIRECTORY_MISSING = "directory-missing"


# meiva:note
# The shell-safe command that starts a row's page server under `python`.
def server_command(python: str, port: int, host: str, serve_dir: str) -> str:
    return shlex.join(
        [python, "-m", PAGE_SERVER_MODULE, "--port", str(port), "--host", host, "--dir", serve_dir]
    )


# meiva:note
# The supervised handles whose row is gone or was re-registered with another port, directory or
# session; their sessions are killed and the handles dropped.
def stale_handles(
    wanted: Mapping[PageKey, PageRow], managed: Mapping[PageKey, ServerHandle]
) -> tuple[PageKey, ...]:
    stale: list[PageKey] = []
    for key, handle in managed.items():
        row = wanted.get(key)
        if (
            row is None
            or row.port != handle.port
            or row.serve_dir != handle.serve_dir
            or row.session_name != handle.session_name
        ):
            stale.append(key)
    return tuple(stale)


# meiva:note
# The persisted sessions of closed rows that are still alive, in the order the rows listed them.
def sessions_to_close(persisted: Collection[str], live_names: Collection[str]) -> tuple[str, ...]:
    return tuple(name for name in persisted if name in live_names)


# meiva:note
# Adopt a row whose recorded session is alive, create the session of a row that has none, and
# close a row whose recorded session is gone or whose directory is missing. A missing directory
# decides first: no session, existing or new, can serve it.
def admission(row: PageRow, live_names: Collection[str], *, directory_exists: bool) -> Admission:
    if not directory_exists:
        return Admission.DIRECTORY_MISSING
    if row.session_name is None:
        return Admission.CREATE
    if row.session_name not in live_names:
        return Admission.SESSION_LOST
    return Admission.ADOPT


# meiva:note
# Whether the handle is past its spawn window, so a failed probe means the server is unavailable.
def health_due(handle: ServerHandle, now: float) -> bool:
    return now - handle.last_launch_monotonic >= SPAWN_VERIFY_TIMEOUT_S


# meiva:note
# The page key each live shell pid supervises, from the sessions the rows name and the pids the
# pty service reports for its live sessions.
def shell_pids(
    wanted: Mapping[PageKey, PageRow], live_pids: Mapping[str, int]
) -> dict[int, PageKey]:
    session_keys = {
        row.session_name: key for key, row in wanted.items() if row.session_name is not None
    }
    return {pid: session_keys[name] for name, pid in live_pids.items() if name in session_keys}
