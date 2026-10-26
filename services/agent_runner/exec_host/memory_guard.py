# meiva:note
# Memory guard for exec subprocesses: take down a runaway `execute_code` at
# critical system memory pressure, and record the notice next to its result file
# so the agent learns why.
#
# The guard relieves pressure; it does not free memory. A kill is worth the work
# it destroys only when the victim is large, so the largest domain at or below the
# relief floor (`settings.agent.exec_memory_guard_floor_mb`) is reported as skipped
# and left running. 2026-10-26 is why: the guard killed a 0.1 GiB exec and the
# system stayed critical (pressure was still 2 fifty seconds later), so that work
# was lost for no relief — and 39% of that day's pressure samples were already
# critical, which makes the case ordinary rather than a corner.
#
# The notice says why an exec died; it cannot make that exec's writes atomic. An
# agent that must not leave a truncated artifact writes a temporary file and
# renames it: with this guard in place that is a requirement, not an optimization.

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from base.host.memory_pressure import MemorySource, PressureLevel, host_memory_source
from base.host.proc import EXEC_DOMAIN_SESSION_ENTRIES
from base.log import logger
from base.native_process.exec_kill_notice import write_notice

POLL_INTERVAL_S = 5.0
_GIB = 1024**3


@dataclass(frozen=True)
class ExecDomain:
    pid: int
    create_time: float
    agent_id: int | None
    result_path: Path
    footprint: int


def kill_notice(domain: ExecDomain, running: int) -> str:
    return (
        "[exec killed by the host memory guard: the system reached critical memory "
        f"pressure; this exec was the largest at {domain.footprint / _GIB:.1f} GiB of "
        f"{running} running. Process less data at a time: split the work into batches "
        "and stream or chunk reads instead of loading everything, then run it again.]"
    )


def select_victim(pressure: str, candidates: Sequence[ExecDomain]) -> tuple[ExecDomain, str] | None:
    if pressure != "critical" or not candidates:
        return None
    victim = max(candidates, key=lambda domain: (domain.footprint, domain.pid))
    return victim, kill_notice(victim, len(candidates))


def _is_exec_leader(process: psutil.Process) -> bool:
    try:
        return process.pid == os.getsid(process.pid) and any(
            argument in EXEC_DOMAIN_SESSION_ENTRIES for argument in process.cmdline()
        )
    except (psutil.Error, OSError):
        return False


def find_exec_domains(host_pid: int, source: MemorySource) -> list[ExecDomain]:
    try:
        descendants = psutil.Process(host_pid).children(recursive=True)
    except psutil.Error:
        return []
    leaders = [process for process in descendants if _is_exec_leader(process)]
    members: dict[int, list[int]] = {process.pid: [] for process in leaders}
    for process in psutil.process_iter():
        with contextlib.suppress(psutil.Error, OSError):
            group = os.getpgid(process.pid)
            if group in members:
                members[group].append(process.pid)
    domains: list[ExecDomain] = []
    for leader in leaders:
        try:
            env = leader.environ()
            raw_agent_id = env.get("MEIVA_AGENT_ID")
            result_path = Path(env["MEIVA_EXEC_RESULT_FILE"])
            created = leader.create_time()
        except (psutil.Error, KeyError, OSError):
            continue
        domains.append(
            ExecDomain(
                pid=leader.pid,
                create_time=created,
                agent_id=int(raw_agent_id) if raw_agent_id else None,
                result_path=result_path,
                footprint=sum(source.footprint(pid) for pid in members[leader.pid]),
            )
        )
    return domains


def kill_domain_leader(domain: ExecDomain) -> None:
    try:
        process = psutil.Process(domain.pid)
        if process.create_time() != domain.create_time:
            return
        process.kill()
    except psutil.NoSuchProcess:
        return


class ExecMemoryGuard:
    def __init__(
        self,
        source: MemorySource,
        *,
        domains: Callable[[], list[ExecDomain]],
        floor_bytes: int,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._source = source
        self._domains = domains
        self._floor_bytes = floor_bytes
        self._interval_s = interval_s
        self._skipped_pid: int | None = None

    def check_once(self) -> ExecDomain | None:
        level = self._source.pressure()
        if level != "critical":
            self._skipped_pid = None
            return None
        candidates = self._domains()
        chosen = select_victim(level, candidates)
        if chosen is None:
            return None
        victim, notice = chosen
        if victim.footprint <= self._floor_bytes:
            self._report_skip(victim, len(candidates), level)
            return None
        write_notice(victim.result_path, notice)
        kill_domain_leader(victim)
        logger.warning(
            "[{label}] {notice}",
            label="exec-memory-guard",
            notice=notice,
            event="exec_memory_guard_killed",
            agent_id=victim.agent_id,
            pid=victim.pid,
            footprint_bytes=victim.footprint,
            running=len(candidates),
            pressure=level,
        )
        return victim

    # meiva:note
    # Critical pressure with nothing worth killing is still news, once per episode.
    #
    # The loop ticks every few seconds while a machine can sit critical for hours, so
    # the same skip is reported once: again only after the largest exec has changed,
    # which is the fact that would change a reader's mind. Silence here would hide the
    # guard's most common outcome (2026-10-26: 39% of samples critical, one kill).
    def _report_skip(self, largest: ExecDomain, running: int, level: PressureLevel) -> None:
        if self._skipped_pid == largest.pid:
            return
        self._skipped_pid = largest.pid
        logger.info(
            "[{label}] largest exec is {footprint:.1f} GiB, at or below the {floor:.1f} GiB"
            " relief floor — nothing killed",
            label="exec-memory-guard",
            event="exec_memory_guard_skipped",
            agent_id=largest.agent_id,
            pid=largest.pid,
            footprint=largest.footprint / _GIB,
            floor=self._floor_bytes / _GIB,
            footprint_bytes=largest.footprint,
            floor_bytes=self._floor_bytes,
            running=running,
            pressure=level,
            free_swap_bytes=psutil.swap_memory().free,
        )

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.check_once)
            except Exception:
                logger.exception("[exec-memory-guard] tick failed — retrying next interval")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue


async def run_memory_guard_forever(stop: asyncio.Event, *, host_pid: int, floor_bytes: int) -> None:
    source = host_memory_source()
    if source is None:
        logger.info("[exec-host] exec memory guard idle — this OS reports no memory pressure state")
        return
    await ExecMemoryGuard(
        source, domains=lambda: find_exec_domains(host_pid, source), floor_bytes=floor_bytes
    ).run_forever(stop)
