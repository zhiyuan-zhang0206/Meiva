# meiva:note
# Memory guard for exec subprocesses: relieve critical system memory pressure
# by killing the one largest `execute_code` process domain owned by exec-host,
# and record the notice next to its result file so the agent learns why.

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from base.host.memory_pressure import MemorySource, host_memory_source
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
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._source = source
        self._domains = domains
        self._interval_s = interval_s

    def check_once(self) -> ExecDomain | None:
        level = self._source.pressure()
        candidates = self._domains() if level == "critical" else []
        chosen = select_victim(level, candidates)
        if chosen is None:
            return None
        victim, notice = chosen
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


async def run_memory_guard_forever(stop: asyncio.Event, *, host_pid: int) -> None:
    source = host_memory_source()
    if source is None:
        logger.info("[exec-host] exec memory guard idle — this OS reports no memory pressure state")
        return
    await ExecMemoryGuard(source, domains=lambda: find_exec_domains(host_pid, source)).run_forever(
        stop
    )
