from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import psutil


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
