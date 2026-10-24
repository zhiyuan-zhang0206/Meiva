# meiva:note
# Decisions about one agent's persistent-shell sessions, from facts the shell already read.
#
# A shell session is named `meiva-agent-<agent_id>-shell-<session_id>[-<label>]`; the label
# names a watcher (`watcher`), a page-server session (`page-<slug>`) or anything the agent
# chose. The shell lists the host's sessions and calls the backend; this module parses the
# listing, picks the sessions an operation targets and judges the outcome, and does no I/O.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.api_contracts.agent_contract import ShellKillMode
from ops.cluster_status.model import AGENT_SHELL_RE
from ops.rpc_schemas import SessionInfo, ShellInfo

__all__ = [
    "ListedShell",
    "confirm_kill_all",
    "find_shell",
    "kill_all_targets",
    "kill_one_result",
    "listed_shells",
]


# meiva:note
# One live shell of an agent: the wire record, and the backend session name the listing
# returned for it. The name is the one the backend knows the session by, so an operation acts on
# exactly the session that was listed.
@dataclass(frozen=True)
class ListedShell:
    session_name: str
    shell: ShellInfo


# meiva:note
# The agent's explicitly created shells and its watchers among the listed sessions, newest id
# last: only `…-agent-<agent_id>-shell-<sid>[-<label>]` names. The agent's own process session
# has no `-shell-` segment and another agent's shells carry another id, so neither is selected.
def listed_shells(sessions: Sequence[SessionInfo], agent_id: int) -> tuple[ListedShell, ...]:
    found: list[ListedShell] = []
    for session in sessions:
        match = AGENT_SHELL_RE.search(session.name)
        if match is None or int(match.group(1)) != agent_id:
            continue
        shell = ShellInfo(
            id=int(match.group(2)),
            name=match.group(3),
            created_at=session.created_at,
            uptime_seconds=session.uptime_seconds,
        )
        found.append(ListedShell(session_name=session.name, shell=shell))
    return tuple(sorted(found, key=lambda listed: listed.shell.id))


# meiva:note
# The listed shell with this session id, or None when the agent has no such live shell here.
def find_shell(listed: Sequence[ListedShell], session_id: int) -> ListedShell | None:
    return next((entry for entry in listed if entry.shell.id == session_id), None)


# meiva:note
# The shells an owner-level kill-all targets: every listed shell below the exclusive cutoff,
# except the page-server sessions in `page_ids` (a page keeps its own lifecycle). Ids at or above
# the cutoff are never targeted.
def kill_all_targets(
    listed: Sequence[ListedShell], before_session_index: int, page_ids: frozenset[int]
) -> tuple[ListedShell, ...]:
    return tuple(
        entry
        for entry in listed
        if entry.shell.id < before_session_index and entry.shell.id not in page_ids
    )


# meiva:note
# The ids a kill-all removed, once the backend answered for every target.
#
# `confirmed` is the backend's verdict per target, in target order. Every selected session was
# attempted; a kill the backend could not confirm raises one error naming those ids after the
# others ran. The backend's kill is idempotent, so a session that ended between the listing and
# its kill still counts as confirmed — it is gone either way.
#
# Raises:
#     RuntimeError: at least one target was not confirmed gone.
#     ValueError: the verdicts do not pair one-to-one with the targets.
def confirm_kill_all(
    agent_id: int, targets: Sequence[ListedShell], confirmed: Sequence[bool]
) -> tuple[int, ...]:
    failed = [t.shell.id for t, ok in zip(targets, confirmed, strict=True) if not ok]
    if failed:
        raise RuntimeError(f"failed to kill shell session(s) {failed} of agent {agent_id}")
    return tuple(t.shell.id for t in targets)


# meiva:note
# The verdict of killing one listed shell: `(mode, interrupted, name)`.
#
# `ok` is the backend's confirmation that the session is gone; `interrupted` says the kill cut
# running work short, which is what makes the TTL reaper notify the owner. A kill the backend
# could not confirm is an error, not a verdict.
#
# Raises:
#     RuntimeError: the backend could not confirm the session gone.
def kill_one_result(
    target: ListedShell, *, ok: bool, interrupted: bool
) -> tuple[ShellKillMode, bool, str | None]:
    if not ok:
        raise RuntimeError(f"failed to kill session {target.session_name!r}")
    return ShellKillMode.KILLED, interrupted, target.shell.name
