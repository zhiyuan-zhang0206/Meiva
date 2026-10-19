from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import psutil

from base.log import logger


class ChildProbe(Protocol):
    def descendants(self, pid: int) -> dict[int, str]: ...

    def is_alive(self, pid: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    command: str


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

    def is_alive(self, pid: int) -> bool:
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False


def _command(process: psutil.Process) -> str:
    try:
        parts = process.cmdline()
    except psutil.Error:
        parts = []
    return " ".join(parts)[:200]


def client_pid(client: Any) -> int | None:
    transport = getattr(client, "_transport", None)
    if transport is None:
        return None
    process = getattr(transport, "_process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) else None


def snapshot(probe: ChildProbe, pid: int | None) -> dict[int, str]:
    if pid is None:
        return {}
    return probe.descendants(pid)


async def wait_gone(
    probe: ChildProbe, pids: Iterable[int], *, timeout_seconds: float, poll_seconds: float
) -> tuple[bool, float]:
    watched = list(pids)
    started = time.monotonic()
    if not watched:
        return (True, 0.0)
    while True:
        if not any(probe.is_alive(pid) for pid in watched):
            return (True, (time.monotonic() - started) * 1000.0)
        if time.monotonic() - started >= timeout_seconds:
            return (False, (time.monotonic() - started) * 1000.0)
        await asyncio.sleep(poll_seconds)


def describe(observed: Mapping[int, str], pids: Iterable[int]) -> tuple[ProcessObservation, ...]:
    return tuple(ProcessObservation(pid=pid, command=observed.get(pid, "")) for pid in sorted(pids))


# meiva:note
# A session's process tree plus its root, as `{pid: command}`. `descendants` answers a
# different question — what the process started — and a close has to wait for both, since
# the root is what holds the transcript.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def watch_tree(probe: ChildProbe, pid: int | None) -> dict[int, str]:
    watched = snapshot(probe, pid)
    if pid is not None:
        watched[pid] = "cli"
    return watched


# meiva:note
# Closing a session has to answer the question `interrupt()` answers: is the process
# tree actually gone? The transport's close asks the CLI to exit and the SDK reaps the
# CLI itself, but a tool's grandchildren can outlive both — and a session that is
# "closed" while its CLI still holds the transcript is the one state the next session
# on that agent must not race. The wait is bounded by the adapter's own settle window,
# and what is left over is reported, never rounded up to success.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
async def settle_close(
    probe: ChildProbe,
    observed: Mapping[int, str],
    harness: str,
    timeout_seconds: float,
    *,
    agent_id: int | None,
    poll_seconds: float = 0.02,
) -> bool | None:
    if not observed:
        # meiva:note
        # Nothing observed is not nothing running. The pid comes from a provider SDK's
        # private transport structure, so a shape change leaves it permanently None and
        # an "all gone" report would be a zero-value fallback dressed up as a total.
        # `interrupt()` already answers `settled=None` here; a close answers the same.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
        probe, observed, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds
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
    return False
