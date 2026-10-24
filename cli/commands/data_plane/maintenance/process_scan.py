# meiva:note
# Which data-plane processes on this host the maintenance stop has not recorded.
#
# A missing pidfile or a refused port is not evidence of no local process, so the stop also
# scans the process table. This module judges that scan from values the shell read, and
# separates three answers per candidate:
#
# - **This home's and not recorded**: a refusal. Something of this home runs that the stop
#   cannot account for.
# - **Another home's**: not this stop's business. A co-located cluster's Postgres, Redis and
#   pooler share the process names.
# - **Cannot tell**: the fact that attributes the process to a home (its working directory, or
#   for a pooler its configuration file) is unreadable. Proving no unrecorded process exists is
#   what the scan is for, so an unreadable candidate refuses with its pid instead of passing
#   or being guessed at. Treating it as another home's would let the one process the scan
#   cannot see through pass.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["SCANNED_NAMES", "ScanVerdict", "SeenProcess", "judge", "refusal", "service_of"]

SCANNED_NAMES = ("postgres", "postmaster", "redis-server", "pgbouncer")


# meiva:note
# One candidate process as the shell read it.
#
# `locator` is the resolved path that attributes the process to a home: its working directory
# for Postgres and Redis, its configuration file for a pooler. It is None when that path could
# not be read, and "" when a pooler names no configuration file at all (it cannot be this
# home's, which always starts with `-d <ini>`). `other_user` and `exiting` (zombie or dead)
# processes are not candidates.
@dataclass(frozen=True)
class SeenProcess:
    pid: int
    name: str
    other_user: bool
    exiting: bool
    locator: str | None


# meiva:note
# The scan's answer: the unrecorded processes of this home, and the candidates that cannot be
# attributed to any home.
@dataclass(frozen=True)
class ScanVerdict:
    unrecorded: tuple[SeenProcess, ...]
    undeterminable: tuple[SeenProcess, ...]


# meiva:note
# The service a process name belongs to, or None for a process the stop does not care about.
def service_of(name: str) -> str | None:
    if name in ("postgres", "postmaster"):
        return "postgres"
    if name == "redis-server":
        return "redis"
    if name == "pgbouncer":
        return "pgbouncer"
    return None


# meiva:note
# Judge the scan.
#
# `homes` maps each service to the resolved path this home's process of that service carries
# (the Postgres data directory, the Redis directory, the pooler's ini). `recorded` maps a
# service to the pids of its captured process and descendants: a Postgres worker shares the
# postmaster's data directory, and a recorded process needs no attribution at all.
def judge(
    seen: tuple[SeenProcess, ...],
    *,
    homes: Mapping[str, str],
    recorded: Mapping[str, frozenset[int]],
) -> ScanVerdict:
    unrecorded: list[SeenProcess] = []
    undeterminable: list[SeenProcess] = []
    for process in seen:
        service = service_of(process.name)
        if service is None or process.other_user or process.exiting:
            continue
        if process.pid in recorded.get(service, frozenset()):
            continue
        if process.locator is None:
            undeterminable.append(process)
        elif process.locator == homes[service]:
            unrecorded.append(process)
    return ScanVerdict(tuple(unrecorded), tuple(undeterminable))


def _listing(processes: tuple[SeenProcess, ...]) -> str:
    return ", ".join(f"{service_of(process.name)} pid {process.pid}" for process in processes)


# meiva:note
# The refusal the stop raises, or None when every candidate is accounted for.
#
# An unrecorded process is named first: it is certain, where an unreadable one is not.
def refusal(verdict: ScanVerdict) -> str | None:
    if verdict.unrecorded:
        return (
            "unrecorded or replacement data-plane process prevents maintenance stop "
            f"({_listing(verdict.unrecorded)})"
        )
    if verdict.undeterminable:
        return (
            "cannot tell whether a data-plane process belongs to this home, so the "
            f"maintenance stop cannot prove nothing is unrecorded ({_listing(verdict.undeterminable)}"
            "; its working directory or configuration file is unreadable). End it as its owner "
            "or retry once it can be read."
        )
    return None
