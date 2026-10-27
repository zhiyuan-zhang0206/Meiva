# meiva:note
# This host's live sessions and the operations on an agent's persistent shells.
#
# The service and daemon sessions come from `get_backend()` (the native supervisor), the
# agents' persistent shells and watchers from `get_shell_backend()` (the pty-sessions service)
# — the two namespaces `meiva start` and the healthchecks write into. Every session name is
# filtered to this cluster's `meiva-` prefix, so dev-worktree clusters and bare non-meiva
# sessions are excluded.
#
# **Host scope: this machine's session backends only.** The gateway's inspector and
# shell-monitor endpoints call these for agents that run on THIS host and dispatch the
# `shell_probe` / `shell_capture` / `shell_kill` ops to the agent's own machine otherwise — a
# remote runner's shells never appear in a local probe.
#
# What the listing means (which sessions are an agent's shells, which a kill-all targets, what a
# kill's outcome is) is decided in `listing` and `shell_plans`; this module reads the backends
# and executes.

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from base.api_contracts.agent_contract import ShellKillMode
from base.cluster import session_name
from base.sessions.backend import get_shell_backend
from base.sessions.page_session import is_page_label
from base.sessions.protocol import SessionBackend
from ops.cluster_status.listing import SessionStart, session_rows
from ops.cluster_status.model import trim_captured_lines
from ops.cluster_status.shell_plans import (
    ListedShell,
    confirm_kill_all,
    find_shell,
    kill_all_targets,
    kill_one_result,
    listed_shells,
)
from ops.rpc_schemas import SessionInfo, ShellInfo

__all__ = [
    "ShellNotFoundError",
    "agent_shell_sessions",
    "capture_shell",
    "collect_sessions",
    "kill_agent_shells",
    "kill_shell",
    "kill_with_verdict",
]

_log = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# meiva:note
# Session names are `meiva-<service>` (see `base.cluster.session_name`); the per-home
# session-record namespace already scopes them to this cluster, and the prefix filter drops
# non-meiva sessions.
def _cluster_prefix() -> str:
    return session_name("")


# meiva:note
# Parallel kills bound one kill-all to roughly one PTY CLI round trip: the synchronous
# terminate path answers inside the gateway's lifecycle deadline.
_KILL_ALL_WORKERS = 8


# meiva:note
# No live shell with this session id exists on this host.
class ShellNotFoundError(LookupError):
    pass


def _starts_of(backend: SessionBackend, names: list[str]) -> tuple[SessionStart, ...]:
    # meiva:note
    # Batch timestamp read: the PTY backend's per-session path costs one CLI process per
    # session (~150 ms each) and a snapshot fans out over ALL of them serially — 28 sessions
    # measured ~4.5 s, blowing past the roster's 3 s probe timeout (2026-08-12, misreported
    # machine-1 offline).
    epochs = backend.session_started_ats(names)
    return tuple(SessionStart(name, epochs.get(name)) for name in names)


# meiva:note
# The backend's sessions for this cluster. A backend that cannot answer raises: an operation
# keyed on "is this shell there" must not read a failed listing as an empty one, or the TTL
# reaper would take a live shell for already gone and drop its deadline.
def _read_starts(backend: SessionBackend) -> tuple[SessionStart, ...]:
    return _starts_of(backend, backend.list_sessions(_cluster_prefix()))


def _degraded_starts(backend: SessionBackend) -> tuple[SessionStart, ...]:
    try:
        names = backend.list_sessions(_cluster_prefix())
    except Exception:
        # meiva:note
        # A down backend means no sessions to show, and the status snapshot must stay readable
        # while it is down.
        _log.warning("session enumeration failed on %s", type(backend).__name__, exc_info=True)
        return ()
    return _starts_of(backend, names)


# meiva:note
# Enumerate this host's live sessions from the two session backends, sorted by name. Agent
# processes are not sessions (they are pid records, tracked separately), so bare
# `meiva-agent-<id>` sessions are left out: the rows cover the daemons and the agents'
# persistent shells. A backend that is down degrades to no rows.
def collect_sessions(
    *, service_backend: SessionBackend, shell_backend: SessionBackend, now: datetime
) -> tuple[SessionInfo, ...]:
    return session_rows((*_degraded_starts(service_backend), *_degraded_starts(shell_backend)), now)


def _agent_shells(backend: SessionBackend, agent_id: int, now: datetime) -> tuple[ListedShell, ...]:
    return listed_shells(session_rows(_read_starts(backend), now), agent_id)


# meiva:note
# This host's live persistent-shell sessions for one agent, newest id last: the agent's
# explicitly created shells and its watchers.
def agent_shell_sessions(agent_id: int, *, now: datetime, home: Path) -> list[ShellInfo]:
    return [entry.shell for entry in _agent_shells(get_shell_backend(home=home), agent_id, now)]


# meiva:note
# Capture the terminal tail of one of an agent's persistent shells.
#
# Resolves `session_id` against this host's live shell sessions for the agent and captures the
# last `lines` lines through the shell session backend (the pty-sessions service — its exact-match
# capture pins it to that one session, never a prefix neighbour `shell-3` vs `shell-30`, or its
# `-watcher`). Returns (session_name, captured_lines, created_at, uptime_seconds) — lines
# newline-split with blank padding trimmed; created_at / uptime_seconds come from the resolved
# session record (the launch epoch + probe-time uptime).
#
# Raises:
#     ShellNotFoundError: no live shell with `session_id` on this host.
#     RuntimeError: the capture failed (the session died between the probe and the capture).
def capture_shell(
    agent_id: int, session_id: int, lines: int = 200, *, now: datetime, home: Path
) -> tuple[str, list[str], datetime | None, int]:
    backend = get_shell_backend(home=home)
    target = find_shell(_agent_shells(backend, agent_id, now), session_id)
    if target is None:
        raise ShellNotFoundError(f"agent {agent_id} has no live shell {session_id} on this host")
    try:
        captured = backend.capture_pane(target.session_name, lines)
    except Exception as exc:
        raise RuntimeError(f"session capture on {target.session_name!r} failed: {exc}") from exc
    shell = target.shell
    return (
        target.session_name,
        trim_captured_lines(captured),
        shell.created_at,
        shell.uptime_seconds,
    )


# meiva:note
# Kill one session and say whether the kill cut running work short.
#
# The verdict comes from the same backend call that kills (`kill_session_with_verdict`), so a
# job starting between a separate idle probe and the kill cannot be missed. A backend without
# verdict support reports interrupted=True (fail-open: a session that cannot be proven idle may
# well be running work). Returns `(ok, interrupted)`.
def kill_with_verdict(backend: SessionBackend, name: str) -> tuple[bool, bool]:
    try:
        ok, _mode, interrupted = backend.kill_session_with_verdict(name)
    except NotImplementedError:
        ok, _mode = backend.kill_session(name)
        return ok, True
    return ok, interrupted


# meiva:note
# Kill one host-local persistent shell, or report that it is already absent.
#
# Returns ``(mode, interrupted, name)``: ``mode`` is ``"killed"`` or ``"absent"``;
# ``interrupted`` is True when the killed session carried live processes (a running foreground
# or background job) at kill time — the TTL reaper uses it to decide whether the reclamation
# deserves a notice to the owner (an empty shell's reaping is silent); ``name`` is the shell's
# optional display name.
def kill_shell(
    agent_id: int, session_id: int, *, home: Path
) -> tuple[ShellKillMode, bool, str | None]:
    backend = get_shell_backend(home=home)
    target = find_shell(_agent_shells(backend, agent_id, _EPOCH), session_id)
    if target is None:
        return ShellKillMode.ABSENT, False, None
    ok, interrupted = kill_with_verdict(backend, target.session_name)
    return kill_one_result(target, ok=ok, interrupted=interrupted)


# meiva:note
# Kill listed host-local shells below the owner's cutoff; return their ids.
#
# The `kill_all_shell_sessions` primitive
# (docs/decisions/agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md). The enumeration is
# the one `…-agent-<id>-shell-<sid>[-<name>]` rule, so the agent's explicit shells and its watchers
# go, while another agent's sessions and the agent's own process session are never touched.
# Page-server sessions (`meiva.ui.serve`, the `page-` label owned by `base.sessions.page_session`)
# are spared: a page keeps its own lifecycle. The backend's kill is idempotent, so a session that
# ended between the listing and its kill still counts as killed. No notice is produced here (an
# owner-level kill is silent) and nothing is written to the database: removing `agent_shell_ttls`
# rows is the gateway's part. IDs at or above the exclusive cutoff are never targeted.
# Enumeration happens once; late creation and crash leftovers require operator cleanup. Every
# selected session is attempted; a kill the backend could not confirm raises one RuntimeError
# naming those ids after the others ran.
def kill_agent_shells(agent_id: int, *, before_session_index: int, home: Path) -> list[int]:
    backend = get_shell_backend(home=home)
    listed = _agent_shells(backend, agent_id, _EPOCH)
    page_ids = frozenset(entry.shell.id for entry in listed if is_page_label(entry.shell.name))
    targets = kill_all_targets(listed, before_session_index, page_ids)
    if not targets:
        return []

    def kill(target: ListedShell) -> bool:
        ok, _mode = backend.kill_session(target.session_name, graceful=False)
        return ok

    with ThreadPoolExecutor(max_workers=min(len(targets), _KILL_ALL_WORKERS)) as pool:
        confirmed = list(pool.map(kill, targets))
    return list(confirm_kill_all(agent_id, targets, confirmed))
