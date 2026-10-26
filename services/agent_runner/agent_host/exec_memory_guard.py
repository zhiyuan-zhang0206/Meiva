# meiva:note
# Memory guard for exec subprocesses: take down a runaway `execute_code` at critical
# system memory pressure, and tell its agent why.
#
# The guard relieves pressure; it does not free memory. A kill is worth the work it
# destroys only when the victim is large, so the largest domain at or below the relief
# floor (`settings.agent.exec_memory_guard_floor_mb`) is reported as skipped and left
# running. 2026-10-26 is why: the guard killed a 0.1 GiB exec and the system stayed
# critical (pressure was still 2 fifty seconds later), so that work was lost for no
# relief — and 39% of that day's pressure samples were already critical, which makes
# the case ordinary rather than a corner. exec-host's guard carries the same rule.
#
# The notice says why an exec died; it cannot make that exec's writes atomic. An agent
# that must not leave a truncated artifact writes a temporary file and renames it:
# with this guard in place that is a requirement, not an optimization.
#
# The agent host runs one sequential loop (`ExecMemoryGuard.run_forever`). Each tick
# reads the pressure level the operating system itself reports
# (`base.host.memory_pressure`); only at ``critical`` does it look at the exec
# domains this host owns, pick the one with the largest footprint, record the
# reason next to its result file and kill its session leader. The run that owns
# that domain observes the leader exit exactly as for any external SIGKILL — it
# kills the rest of the process group and reaps the leader — and then reads the
# notice, so the agent's result states the cause instead of a generic "exited
# without a result envelope". The loop sleeps one interval after a kill and
# re-reads the level before it considers another domain, so a recovery that
# follows the first kill is never answered with a second.
#
# Domains are found by the OS, not tracked: the host's descendants that lead a
# session running an exec entry module (`base.host.proc.EXEC_DOMAIN_SESSION_ENTRIES`),
# with the agent id and result path read from the leader's launch environment.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from base.host.env.agent_env import AGENT_ID_ENV
from base.host.memory_pressure import MemorySource, PressureLevel
from base.host.proc import EXEC_DOMAIN_SESSION_ENTRIES
from base.log import logger
from base.native_process.exec_kill_notice import write_notice

POLL_INTERVAL_S = 5.0
_GIB = 1024**3


@dataclass(frozen=True)
class ExecDomain:
    """One running exec process domain and what it holds."""

    pid: int
    create_time: float
    agent_id: int | None
    result_path: Path
    footprint: int


def kill_notice(domain: ExecDomain, running: int) -> str:
    """The text the killed exec's agent reads."""
    return (
        "[exec killed by the host memory guard: the system reached critical memory "
        f"pressure; this exec was the largest at {domain.footprint / _GIB:.1f} GiB of "
        f"{running} running. Process less data at a time: split the work into batches "
        "and stream or chunk reads instead of loading everything, then run it again.]"
    )


def _is_exec_leader(process: psutil.Process) -> bool:
    try:
        return process.pid == os.getsid(process.pid) and any(
            argument in EXEC_DOMAIN_SESSION_ENTRIES for argument in process.cmdline()
        )
    except (psutil.Error, OSError):
        return False


def find_exec_domains(host_pid: int, source: MemorySource) -> list[ExecDomain]:
    """Every exec process domain under `host_pid`, with its group's total footprint."""
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
            raw_agent_id = env.get(AGENT_ID_ENV)
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


# meiva:note
# SIGKILL the domain's session leader if it is still the process we measured.
#
# The run that owns the domain kills the rest of the group when it sees the
# leader exit (`run_in_subprocess`), the same path an external SIGKILL takes.
def kill_domain_leader(domain: ExecDomain) -> None:
    try:
        process = psutil.Process(domain.pid)
        if process.create_time() != domain.create_time:
            return
        process.kill()
    except psutil.NoSuchProcess:
        return


class ExecMemoryGuard:
    """The sequential loop; `source`, `domains` and `kill` are the test seams."""

    def __init__(
        self,
        source: MemorySource,
        *,
        domains: Callable[[], list[ExecDomain]],
        floor_bytes: int,
        kill: Callable[[ExecDomain], None] = kill_domain_leader,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._source = source
        self._domains = domains
        self._floor_bytes = floor_bytes
        self._kill = kill
        self._interval_s = interval_s
        self._skipped_pid: int | None = None

    # meiva:note
    # Kill the largest exec domain when the system is critical and that kill is worth the
    # work it destroys; a domain at or below the floor is reported instead.
    def check_once(self) -> ExecDomain | None:
        level = self._source.pressure()
        if level != "critical":
            self._skipped_pid = None
            return None
        candidates = self._domains()
        if not candidates:
            return None
        victim = max(candidates, key=lambda domain: (domain.footprint, domain.pid))
        if victim.footprint <= self._floor_bytes:
            self._report_skip(victim, len(candidates), level)
            return None
        notice = kill_notice(victim, len(candidates))
        write_notice(victim.result_path, notice)
        self._kill(victim)
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

    async def run_forever(self) -> None:
        """One tick per interval; a failed tick is logged and the loop continues."""
        while True:
            try:
                await asyncio.to_thread(self.check_once)
            except Exception:
                logger.exception("[exec-memory-guard] tick failed — retrying next interval")
            await asyncio.sleep(self._interval_s)


async def run_memory_guard_forever(log: logging.Logger, host_pid: int, floor_bytes: int) -> None:
    """Relieve critical memory pressure by killing the largest exec domain.

    Where the OS reports no pressure state (Linux), the guard does not run.
    """
    from base.host.memory_pressure import host_memory_source

    source = host_memory_source()
    if source is None:
        log.info("[agent-host] exec memory guard idle — this OS reports no memory pressure state")
        return
    await ExecMemoryGuard(
        source, domains=lambda: find_exec_domains(host_pid, source), floor_bytes=floor_bytes
    ).run_forever()
