from __future__ import annotations

import contextlib
import os
from collections.abc import Coroutine, Iterable, Mapping
from typing import Protocol

import psutil

from base.log import logger

SIGTERM = 15
SIGKILL = 9


class ChildProbe(Protocol):
    def descendants(self, pid: int) -> dict[int, str]: ...

    def tool_processes(self, pid: int) -> dict[int, str]: ...

    def is_alive(self, pid: int) -> bool: ...

    def started_at(self, pid: int) -> float | None: ...

    def kill(self, pid: int, sig: int) -> None: ...


class PsutilChildProbe:
    def descendants(self, pid: int) -> dict[int, str]:
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
        except psutil.Error:
            return {}
        observed: dict[int, str] = {}
        for child in children:
            try:
                observed[child.pid] = _command(child)
            except psutil.Error:
                continue
        return observed

    # meiva:note
    # The processes of the CLI's tool calls, told from the CLI's own helpers (MCP servers, the Codex
    # code-mode host). Both CLIs start a tool call's command as a direct child that leads a session of
    # its own, and everything the command starts without detaching stays in that session; a helper stays
    # in the CLI's session, or sits below such a helper. So a tool process is a member of a session led
    # by a direct child of `pid` that is not `pid`'s own session. A process that detaches itself (a
    # daemon, a helper's detached worker) is not a tool process: it was set apart from the call on purpose.
    # Command lines cannot tell them apart (the probe keeps 200 characters and the CLIs wrap the command
    # differently); the session structure can, and was recorded on Claude Code and Codex 0.166.
    # meiva:link base/lm/harness/docs/harness.md
    def tool_processes(self, pid: int) -> dict[int, str]:
        try:
            parent = psutil.Process(pid)
            own_session = os.getsid(pid)
            children = parent.children(recursive=True)
            direct = {child.pid for child in parent.children()}
        except (psutil.Error, OSError):
            return {}
        tools: dict[int, str] = {}
        for child in children:
            try:
                session = os.getsid(child.pid)
            except OSError:
                continue
            if session != own_session and session == child.pid and child.pid in direct:
                tools[child.pid] = _command(child)
        for child in children:
            try:
                session = os.getsid(child.pid)
            except OSError:
                continue
            if session in tools and child.pid not in tools:
                tools[child.pid] = _command(child)
        return tools

    def is_alive(self, pid: int) -> bool:
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    # meiva:note
    # When the process now holding `pid` started, or None when none does. Together with the pid
    # it names one process: a pid the system handed to a later process answers a different time.
    def started_at(self, pid: int) -> float | None:
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            return None

    def kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def _command(process: psutil.Process) -> str:
    try:
        parts = process.cmdline()
    except psutil.Error:
        parts = []
    return " ".join(parts)[:200]


# meiva:note
# The harness SDKs expose neither the child's pid nor its exit status, so this
# module reads them off `client._transport._process`. `client_transport` is the one
# place that private path lives; everything else asks it. `disconnect()` sets
# `_transport = None` before it returns, so a caller that needs the exit status must
# capture the transport (or the process) first and ask afterwards — asking the
# client later answers None, which would read as "never observed".
# meiva:note
# The adapter's answer to `exit_code`, in one place: None until the session closes,
# then whatever its CLI child reported (0 normal, non-zero abnormal). A session sets
# `_exit_code` as it closes and inherits the reader from here, so five adapters cannot
# drift into five slightly different answers.
class ExitStatus:
    _exit_code: int | None = None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code


class SdkProcess(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def returncode(self) -> int | None: ...


class SdkTransport(Protocol):
    @property
    def _process(self) -> SdkProcess | None: ...


class SdkClient(Protocol):
    @property
    def _transport(self) -> object: ...


def client_transport(client: SdkClient | None) -> SdkTransport | None:
    return getattr(client, "_transport", None)


def client_process(client: SdkClient | None) -> SdkProcess | None:
    return getattr(client_transport(client), "_process", None)


# meiva:note
# The child's exit status: None while it runs, or when there is no process.
def process_returncode(process: SdkProcess | None) -> int | None:
    return None if process is None else getattr(process, "returncode", None)


def client_pid(client: SdkClient | None) -> int | None:
    pid = getattr(client_process(client), "pid", None)
    return pid if isinstance(pid, int) else None


def snapshot(probe: ChildProbe, pid: int | None) -> dict[int, str]:
    if pid is None:
        return {}
    return probe.descendants(pid)


def tool_snapshot(probe: ChildProbe, pid: int | None) -> dict[int, str]:
    if pid is None:
        return {}
    return probe.tool_processes(pid)


# meiva:note
# Ends processes a cancel is answerable for: SIGTERM first, SIGKILL for whatever is still running
# after `grace_seconds`. Returns the pids that needed SIGKILL, so the report names what did not
# leave on request. Callers pass pids read from a fresh snapshot, never remembered ones.
# meiva:link base/lm/harness/docs/harness.md
async def terminate(
    probe: ChildProbe,
    pids: Iterable[int],
    *,
    grace_seconds: float,
    poll_seconds: float,
    clock: WaitClock,
) -> tuple[int, ...]:
    targets = [pid for pid in pids if pid > 1]
    for pid in targets:
        probe.kill(pid, SIGTERM)
    gone, _ = await wait_gone(
        probe, targets, timeout_seconds=grace_seconds, poll_seconds=poll_seconds, clock=clock
    )
    if gone:
        return ()
    stubborn = tuple(pid for pid in targets if probe.is_alive(pid))
    for pid in stubborn:
        probe.kill(pid, SIGKILL)
    return stubborn


# meiva:note
# What a wait for processes to exit needs of a clock: the interval to measure and the pause
# between polls. The session's clock satisfies it.
class WaitClock(Protocol):
    def monotonic(self) -> float: ...

    def asleep(self, seconds: float) -> Coroutine[object, object, None]: ...


async def wait_gone(
    probe: ChildProbe,
    pids: Iterable[int],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    clock: WaitClock,
) -> tuple[bool, float]:
    watched = list(pids)
    started = clock.monotonic()
    if not watched:
        return (True, 0.0)
    while True:
        if not any(probe.is_alive(pid) for pid in watched):
            return (True, (clock.monotonic() - started) * 1000.0)
        if clock.monotonic() - started >= timeout_seconds:
            return (False, (clock.monotonic() - started) * 1000.0)
        await clock.asleep(poll_seconds)


# meiva:note
# A session's process tree plus its root, as `{pid: command}`. `descendants` answers a
# different question — what the process started — and a close has to wait for both, since
# the root is what holds the transcript.
# meiva:link base/lm/harness/docs/harness.md
def watch_tree(probe: ChildProbe, pid: int | None) -> dict[int, str]:
    watched = snapshot(probe, pid)
    if pid is not None:
        watched[pid] = "cli"
    return watched


# meiva:note
# A close that left processes running names them, together with the probe that saw them, so whoever
# fences the next session on that agent can ask again later instead of trusting a snapshot taken at
# the end of the settle window: the fence holds while one of these processes is alive and lifts once
# none is (task #7074). Each is remembered as its pid and start time, so a pid the system has since
# handed to an unrelated process counts as exited rather than holding the fence up.
# meiva:link base/lm/harness/docs/harness.md
class CloseResidueError(RuntimeError):
    def __init__(
        self,
        harness: str,
        agent_id: int | None,
        remaining: Mapping[int, float | None],
        probe: ChildProbe,
    ) -> None:
        super().__init__(
            f"{harness} session for agent {agent_id} left {len(remaining)} process(es) running"
        )
        self.remaining = dict(remaining)
        self._probe = probe

    def running(self) -> tuple[int, ...]:
        return tuple(
            pid
            for pid, started in self.remaining.items()
            if self._probe.is_alive(pid) and self._probe.started_at(pid) == started
        )


class ProcessFamily:
    def __init__(
        self, root_pid: int, probe: ChildProbe, *, uid: int, marker: tuple[str, str]
    ) -> None:
        self._root = root_pid
        self._probe = probe
        self._started = probe.started_at(root_pid)
        self._known = {root_pid: self._started}
        self._marker = marker
        self._uid = uid

    def observe(self) -> None:
        if self._same_root():
            self._capture(self._probe.descendants(self._root))

    def observe_session(self, *, include_marked: bool) -> None:
        self.observe()
        current = self._probe.started_at(self._root)
        original_session = self._started is not None and (
            current is None or current == self._started
        )
        for process in psutil.process_iter():
            try:
                if process.uids().real != self._uid:
                    continue
                started = process.create_time()
                same_session = original_session and _session_id(process.pid) == self._root
                if same_session or (
                    include_marked and process.environ().get(self._marker[0]) == self._marker[1]
                ):
                    self._known[process.pid] = started
            except (psutil.Error, OSError, NotImplementedError):
                continue

    def _same_root(self) -> bool:
        return self._started is not None and self._probe.started_at(self._root) == self._started

    def _capture(self, pids: Iterable[int]) -> None:
        for pid in pids:
            started = self._probe.started_at(pid)
            if self._started is not None and started is not None and started >= self._started:
                self._known[pid] = started

    def kill(self) -> None:
        for pid in self._known:
            if pid != self._root and self._is_member(pid):
                with contextlib.suppress(PermissionError):
                    self._probe.kill(pid, SIGKILL)

    def _is_member(self, pid: int) -> bool:
        return self._probe.is_alive(pid) and self._probe.started_at(pid) == self._known[pid]

    async def settle(
        self,
        *,
        harness: str,
        include_marked: bool,
        agent_id: int | None,
        timeout: float,
        clock: WaitClock,
        poll_seconds: float,
    ) -> None:
        self.observe_session(include_marked=include_marked)
        self.kill()
        deadline = clock.monotonic() + timeout
        remaining = self._remaining()
        while remaining and clock.monotonic() < deadline:
            await clock.asleep(poll_seconds)
            remaining = self._remaining()
        if remaining:
            raise CloseResidueError(harness, agent_id, remaining, self._probe)

    def _remaining(self) -> dict[int, float | None]:
        return {pid: started for pid, started in self._known.items() if self._is_member(pid)}


def _session_id(pid: int) -> int | None:
    try:
        return os.getsid(pid)
    except OSError:
        return None


# meiva:note
# Closing a session has to answer the question `interrupt()` answers: is the process
# tree actually gone? The transport's close asks the CLI to exit and the SDK reaps the
# CLI itself, but a tool's grandchildren can outlive both — and a session that is
# "closed" while its CLI still holds the transcript is the one state the next session
# on that agent must not race. The wait is bounded by the adapter's own settle window,
# and what is left over is reported, never rounded up to success.
# meiva:link base/lm/harness/docs/harness.md
async def settle_close(
    probe: ChildProbe,
    observed: Mapping[int, str],
    harness: str,
    timeout_seconds: float,
    *,
    agent_id: int | None,
    clock: WaitClock,
    poll_seconds: float = 0.02,
) -> bool | None:
    if not observed:
        # meiva:note
        # Nothing observed is not nothing running. The pid comes from a provider SDK's
        # private transport structure, so a shape change leaves it permanently None and
        # an "all gone" report would be a zero-value fallback dressed up as a total.
        # `interrupt()` already answers `settled=None` here; a close answers the same.
        # meiva:link base/lm/harness/docs/harness.md
        logger.info(
            "[{label}] {body}",
            label="harness",
            event="harness_close_unobserved",
            body=f"{harness} session closed but its process tree was never observed",
            harness=harness,
            agent_id=agent_id,
        )
        return None
    gone, waited_ms = await wait_gone(
        probe, observed, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds, clock=clock
    )
    if gone:
        logger.info(
            "[{label}] {body}",
            label="harness",
            event="harness_close_settled",
            body=f"{harness} session closed; {len(observed)} process(es) observed, none left",
            harness=harness,
            agent_id=agent_id,
            waited_ms=round(waited_ms, 1),
        )
        return True
    remaining = [pid for pid in observed if probe.is_alive(pid)]
    logger.warning(
        "[{label}] {body}",
        label="harness",
        event="harness_close_residue",
        body=f"{harness} session for agent {agent_id} left {len(remaining)} process(es) running",
        harness=harness,
        agent_id=agent_id,
        remaining=len(remaining),
        waited_ms=round(waited_ms, 1),
    )
    raise CloseResidueError(
        harness, agent_id, {pid: probe.started_at(pid) for pid in remaining}, probe
    )
