"""Supervise a coding agent through its durable work file.

Generic use wakes the launching Meiva agent on actionable status or a stall.
Canonical Codex use additionally owns terminal cleanup: DONE, HANDOFF, owner
termination, process death, expiry, or work-file deletion closes the recorded
PTY and reclaims its generation state directory before this process exits.

Each wake is sent once; the SDK's own bounded retries are the only retry. A
send that still fails is logged: the one-shot call sites exit 2 (the launcher
gets an exit notice), while the supervision loop keeps its schedule and sends
again at its next trigger. A wake that lands in a gateway / agent restart window
is not redelivered.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, assert_never
from uuid import uuid4

import httpx

import meiva
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, GatewayUnavailable
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus

WORK_FILE = "/path/to/work.md"
POLL_SECONDS = 60
STALL_SECONDS = 600
HEARTBEAT_SECONDS = 480
# 2h: a supervision run is bounded — after this the launching agent is woken
# with the limit notice and may relaunch; without it a forgotten runner polls
# forever (task #3696 exception inventory).
HARD_LIMIT_SECONDS = 7200

ACTIONABLE = ("DONE", "NEED_INPUT", "HANDOFF")
_STATUS = re.compile(r"^STATUS:\s*(\w+)", re.MULTILINE)


# meiva:note
# The pace of one supervision run: how long to sleep between polls, how long an unchanged work file counts as a stall, how long between heartbeat wakes, and the bound after which a generic run wakes its launcher and ends. The defaults are the constants above, which an agent raises in its copy of this file for a slow model; the verdicts below are functions of this value, never of the module constants.
@dataclass(frozen=True)
class Cadence:
    poll_seconds: float = POLL_SECONDS
    stall_seconds: float = STALL_SECONDS
    heartbeat_seconds: float = HEARTBEAT_SECONDS
    hard_limit_seconds: float = HARD_LIMIT_SECONDS


DEFAULT_CADENCE = Cadence()


# meiva:note
# The first STATUS token of a work file's text, or None when there is none.
def parse_status(text: str) -> str | None:
    matches = _STATUS.findall(text)
    return matches[0] if matches else None


def read_status(path: str) -> str | None:
    """Return the first STATUS token, or None when absent/unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return parse_status(text)


def terminal_reason(
    status: str | None,
    *,
    status_is_current: bool,
    owner_terminated: bool,
    session_crashed: bool,
    expired: bool,
    work_file_deleted: bool,
    hard_limit_reached: bool,
) -> str | None:
    """Pure terminal-decision contract, ordered by semantic owner intent."""
    if status_is_current and status == "DONE":
        return "collaboration-done"
    if status_is_current and status == "HANDOFF":
        return "collaboration-handoff"
    if owner_terminated:
        return "owner-terminated"
    if session_crashed:
        return "session-crashed"
    if expired:
        return "expired"
    if work_file_deleted:
        return "work-file-deleted"
    if hard_limit_reached:
        return "supervisor-hard-limit"
    return None


# meiva:note
# What the gateway answered when asked about the owner agent. A gateway that cannot answer proves nothing about the owner: the PTY liveness and the absolute expiry checks remain local and keep protecting the generation.
class OwnerRead(StrEnum):
    ALIVE = "alive"
    TERMINATED = "terminated"
    NOT_FOUND = "not-found"
    UNAVAILABLE = "unavailable"


# meiva:note
# Whether the answer shows the owner gone: a terminated or unknown agent is, an unavailable gateway is not.
def owner_terminated(read: OwnerRead) -> bool:
    if read is OwnerRead.TERMINATED or read is OwnerRead.NOT_FOUND:
        return True
    if read is OwnerRead.ALIVE or read is OwnerRead.UNAVAILABLE:
        return False
    assert_never(read)


# meiva:note
# The log line an owner read earns and whether the gateway is now down: an outage that spans many polls is reported once and its end once.
def gateway_edge(
    was_down: bool, read: OwnerRead, agent_id: int, detail: str
) -> tuple[bool, str | None]:
    if read is OwnerRead.UNAVAILABLE:
        if was_down:
            return True, None
        return True, (
            f"gateway unavailable checking owner agent {agent_id}; treating it as alive: {detail}"
        )
    return False, ("gateway reachable again" if was_down else None)


# meiva:note
# Whether a generation's PTY is gone. A launching generation has crashed when its claimant died before publishing (`launch_stale`); an active one with a recorded session has crashed when that session is not alive (`session_alive`, read only for such a generation); any other state has not crashed.
def session_crashed(
    status: CodingSessionStatus,
    *,
    launch_stale: bool,
    has_session_name: bool,
    session_alive: bool | None,
) -> bool:
    if status == CodingSessionStatus.LAUNCHING:
        return launch_stale
    if status != CodingSessionStatus.ACTIVE or not has_session_name:
        return False
    return not session_alive


# meiva:note
# Whether a canonical generation is already over for this watcher: its record belongs to another generation or is inactive, terminal or invalid, so there is nothing left to supervise.
def generation_over(generation_matches: bool, status: CodingSessionStatus) -> bool:
    return not generation_matches or status in (
        CodingSessionStatus.INACTIVE,
        CodingSessionStatus.TERMINAL,
        CodingSessionStatus.INVALID,
    )


# meiva:note
# What one polling beat saw of the work file and of the clock: its STATUS token and modification time (None when the file is missing), whether it was ever seen, and the seconds since the run began, since the file last changed and since the last wake.
@dataclass(frozen=True)
class Beat:
    status: str | None
    mtime: float | None
    saw_work_file: bool
    elapsed_total: float
    elapsed_change: float
    elapsed_wake: float


# meiva:note
# What a generic watcher remembers between beats: whether the next beat is the first, and the modification time of the actionable status that was already there (or already woke) so the same report never wakes twice.
@dataclass(frozen=True)
class Memory:
    first_poll: bool
    last_actionable_mtime: float | None


class Wake(NamedTuple):
    message: str


# meiva:note
# Record a pre-existing actionable status at arming time as the baseline, not a new report: the immediate wake is suppressed and the watcher waits for a change.
def _baselined(memory: Memory, beat: Beat) -> Memory:
    if not memory.first_poll:
        return memory
    if beat.status in ACTIONABLE:
        return Memory(False, beat.mtime)
    return Memory(False, memory.last_actionable_mtime)


def _actionable_wake(
    status: str, path: str, beat: Beat, memory: Memory, cadence: Cadence
) -> Wake | None:
    if beat.mtime != memory.last_actionable_mtime:
        return Wake(f"coding agent reported STATUS: {status} in {path} -- read that file")
    if beat.elapsed_wake > cadence.heartbeat_seconds:
        return Wake(
            f"coding agent heartbeat: STATUS is {status!r} in {path} (unchanged since arming)"
        )
    return None


def _working_wake(status: str | None, path: str, beat: Beat, cadence: Cadence) -> Wake | None:
    if beat.elapsed_change > cadence.stall_seconds:
        return Wake(
            f"coding agent has been WORKING with no change to {path} for over "
            f"{cadence.stall_seconds}s -- capture its screen"
        )
    if beat.elapsed_wake > cadence.heartbeat_seconds:
        label = status if status else "MISSING"
        return Wake(f"coding agent heartbeat: STATUS is {label!r} in {path}")
    return None


# meiva:note
# The verdict of one beat of a generic (wake-only) watcher, with what it remembers afterwards. The first beat records an actionable status already present as the baseline instead of waking for it; then a deleted work file, the hard limit, an actionable status that is new (or unchanged for a heartbeat), a WORKING or missing status that stalled (or outlived a heartbeat) and any unrecognized status each earn one wake, in that order. A wake ends the run; None means keep polling.
def generic_verdict(
    beat: Beat, memory: Memory, path: str, cadence: Cadence
) -> tuple[Wake | None, Memory]:
    memory = _baselined(memory, beat)
    status = beat.status
    if status is None and beat.saw_work_file and beat.mtime is None:
        deleted = f"work file deleted: {path} -- the coding agent may have removed its workspace"
        return Wake(deleted), memory
    if beat.elapsed_total > cadence.hard_limit_seconds:
        limit = f"hard limit reached while polling {path} for over {cadence.hard_limit_seconds}s"
        return Wake(limit), memory
    if status is not None and status in ACTIONABLE:
        return _actionable_wake(status, path, beat, memory, cadence), memory
    if status is None or status == "WORKING":
        return _working_wake(status, path, beat, cadence), memory
    return Wake(f"coding agent reported unknown STATUS: {status!r} in {path}"), memory


# meiva:note
# What a canonical beat read of the owner record, the gateway and the PTY, once the generation is known to be live: when it was created (a POSIX timestamp), and the three ways it can have ended (the owner agent terminated, the session crashed, the generation expired).
@dataclass(frozen=True)
class OwnerFacts:
    created_at: float | None
    owner_terminated: bool
    session_crashed: bool
    expired: bool


class Terminalize(NamedTuple):
    reason: str


class NeedInput(NamedTuple):
    message: str


class Stalled(NamedTuple):
    message: str


class Quiet(NamedTuple):
    pass


# meiva:note
# The verdict of one beat of a canonical (generation-owning) supervisor. A status counts as current only if the work file was modified after the generation was created. The terminal reasons come first (`terminal_reason`: the persisted generation expiry governs, so the generic hard limit never shortens a task-adapted lease); then a current NEED_INPUT that has not woken yet, then a stall that has outlived a heartbeat; otherwise the beat is quiet.
def canonical_verdict(
    beat: Beat, facts: OwnerFacts, last_actionable_mtime: float | None, path: str, cadence: Cadence
) -> Terminalize | NeedInput | Stalled | Quiet:
    status_is_current = bool(
        beat.mtime is not None and facts.created_at is not None and beat.mtime >= facts.created_at
    )
    reason = terminal_reason(
        beat.status,
        status_is_current=status_is_current,
        owner_terminated=facts.owner_terminated,
        session_crashed=facts.session_crashed,
        expired=facts.expired,
        work_file_deleted=beat.saw_work_file and beat.mtime is None,
        hard_limit_reached=False,
    )
    if reason is not None:
        return Terminalize(reason)
    if beat.status == "NEED_INPUT" and status_is_current and beat.mtime != last_actionable_mtime:
        return NeedInput(
            f"coding agent reported STATUS: NEED_INPUT in {path} -- read the file and reply"
        )
    if (
        beat.elapsed_change > cadence.stall_seconds
        and beat.elapsed_wake > cadence.heartbeat_seconds
    ):
        return Stalled(
            f"coding agent has made no work-file change for {beat.elapsed_change:.0f}s: {path}"
        )
    return Quiet()


def _notify(agent_id: int, content: str, *, canonical: bool) -> bool:
    """Send one supervision message; False when the send failed.

    The one-shot call sites exit 2 on False, while the in-loop call sites keep
    supervising and send again at their next trigger.
    """
    try:
        if canonical:
            meiva.agents.send_system_note(
                agent_id, content, tag="task", resurrect=False, idempotency_key=str(uuid4())
            )
        else:
            meiva.agents.send_message(agent_id, content)
    except Exception as exc:  # a lost wake must be logged
        print(f"wake delivery failed: {exc!r}", flush=True)
        return False
    return True


def _canonical_context(
    *,
    cluster: str | None,
    workspace: str | None,
    generation: str | None,
    owner_agent_id: int | None,
) -> tuple[coding_session_owner.CodingSessionKey, str, int] | None:
    values = (cluster, workspace, generation, owner_agent_id)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("canonical supervision requires cluster, workspace, generation, and owner")
    assert cluster is not None and workspace is not None  # noqa: S101
    assert generation is not None and owner_agent_id is not None  # noqa: S101
    key = coding_session_owner.canonical_key(workspace, tool="codex", cluster=cluster)
    return key, generation, owner_agent_id


def _terminalize(
    key: coding_session_owner.CodingSessionKey,
    generation: str,
    owner_agent_id: int,
    reason: str,
) -> bool:
    try:
        stopped = coding_session_owner.terminate_generation(
            key, generation, reason=reason, writer=AtomicWriter(SYSTEM_FS)
        )
    except Exception as exc:
        print(f"terminalizing generation {generation} ({reason}) failed: {exc!r}", flush=True)
        if reason != "owner-terminated":
            _notify(
                owner_agent_id,
                f"Codex cleanup failed for generation {generation}: {exc}",
                canonical=True,
            )
        return False
    if stopped and reason != "owner-terminated":
        _notify(
            owner_agent_id,
            f"Codex generation {generation} terminalized ({reason}).",
            canonical=True,
        )
    return stopped


# meiva:note
# Ask the gateway for the owner agent's status. A gateway that cannot answer is `UNAVAILABLE`: an unreachable one (`GatewayUnavailable`) and one that answers 5xx after the SDK's bounded retries alike, because either would otherwise end the supervisor on a traceback and leave the generation unsupervised. Any other refusal (an auth or request error) is a defect and propagates.
def _read_owner(agent_id: int) -> tuple[OwnerRead, str]:
    try:
        status = meiva.agents.get_status(agent_id)
    except AgentNotFound:
        return OwnerRead.NOT_FOUND, ""
    except GatewayUnavailable as exc:
        return OwnerRead.UNAVAILABLE, repr(exc)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise
        return OwnerRead.UNAVAILABLE, repr(exc)
    return (OwnerRead.TERMINATED if status is AgentStatus.TERMINATED else OwnerRead.ALIVE), ""


def _session_alive(owner: coding_session_owner.CodingSessionOwner) -> bool | None:
    if owner.status != CodingSessionStatus.ACTIVE or owner.session_name is None:
        return None
    from base.sessions.backend import get_shell_backend

    return get_shell_backend().has_session(owner.session_name)


# meiva:note
# One polling session: the clock readings the watcher keeps and the verdicts it executes.
class _Watch:
    def __init__(
        self,
        path: str,
        target_agent: int,
        canonical_context: tuple[coding_session_owner.CodingSessionKey, str, int] | None,
        cadence: Cadence,
        now: float,
    ) -> None:
        self.path = path
        self.target_agent = target_agent
        self.canonical_context = canonical_context
        self.cadence = cadence
        self.start_time = now
        self.last_change = now
        self.last_wake = now
        self.last_mtime: float | None = None
        self.memory = Memory(first_poll=True, last_actionable_mtime=None)
        self.gateway_down = False
        self.saw_work_file = Path(path).exists()

    def poll(self, now: float, wall: dt.datetime) -> bool:
        """One polling beat; True when the watch is over (the caller sleeps otherwise)."""
        try:
            mtime: float | None = Path(self.path).stat().st_mtime
            self.saw_work_file = True
        except FileNotFoundError:
            mtime = None
        if mtime != self.last_mtime:
            self.last_mtime = mtime
            self.last_change = now
        beat = Beat(
            status=read_status(self.path),
            mtime=mtime,
            saw_work_file=self.saw_work_file,
            elapsed_total=now - self.start_time,
            elapsed_change=now - self.last_change,
            elapsed_wake=now - self.last_wake,
        )
        if self.canonical_context is not None:
            return self._canonical_poll(beat, now, wall)
        return self._generic_poll(beat)

    def _owner_terminated(self, agent_id: int) -> bool:
        read, detail = _read_owner(agent_id)
        self.gateway_down, line = gateway_edge(self.gateway_down, read, agent_id, detail)
        if line is not None:
            print(line, flush=True)
        return owner_terminated(read)

    def _canonical_poll(self, beat: Beat, now: float, wall: dt.datetime) -> bool:
        assert self.canonical_context is not None  # noqa: S101
        key, expected_generation, canonical_owner = self.canonical_context
        owner = coding_session_owner.read(key, expected_generation)
        if generation_over(owner.generation == expected_generation, owner.status):
            return True
        facts = OwnerFacts(
            created_at=None if owner.created_at is None else owner.created_at.timestamp(),
            owner_terminated=self._owner_terminated(canonical_owner),
            session_crashed=session_crashed(
                owner.status,
                launch_stale=coding_session_owner.launch_is_stale(owner, now=wall),
                has_session_name=owner.session_name is not None,
                session_alive=_session_alive(owner),
            ),
            expired=owner.expires_at is not None and wall >= owner.expires_at,
        )
        verdict = canonical_verdict(
            beat, facts, self.memory.last_actionable_mtime, self.path, self.cadence
        )
        if isinstance(verdict, Terminalize):
            return _terminalize(key, expected_generation, canonical_owner, verdict.reason)
        if isinstance(verdict, NeedInput):
            _notify(self.target_agent, verdict.message, canonical=True)
            self.memory = Memory(self.memory.first_poll, beat.mtime)
            self.last_wake = now
        elif isinstance(verdict, Stalled):
            _notify(self.target_agent, verdict.message, canonical=True)
            self.last_wake = now
        elif not isinstance(verdict, Quiet):
            assert_never(verdict)
        return False

    def _generic_poll(self, beat: Beat) -> bool:
        wake, self.memory = generic_verdict(beat, self.memory, self.path, self.cadence)
        if wake is None:
            return False
        if not _notify(self.target_agent, wake.message, canonical=False):
            raise SystemExit(2)
        return True


def watch(
    path: str,
    *,
    cluster: str | None = None,
    workspace: str | None = None,
    generation: str | None = None,
    owner_agent_id: int | None = None,
    cadence: Cadence = DEFAULT_CADENCE,
) -> None:
    """Poll until generic wake or canonical generation terminalization."""
    canonical_context = _canonical_context(
        cluster=cluster,
        workspace=workspace,
        generation=generation,
        owner_agent_id=owner_agent_id,
    )
    target_agent = owner_agent_id if owner_agent_id is not None else meiva.self.AGENT_ID
    session = _Watch(path, target_agent, canonical_context, cadence, time.monotonic())
    while not session.poll(time.monotonic(), dt.datetime.now(dt.UTC)):
        time.sleep(cadence.poll_seconds)


if __name__ == "__main__":
    watch(WORK_FILE)
