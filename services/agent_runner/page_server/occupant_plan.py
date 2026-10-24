# meiva:note
# Which local processes hold a page-server port, and which of them the daemon kills.
#
# `daemon.reclaim_occupants` reads the process table and signals processes. What it reads becomes
# values here: the argv of each process, its home, its ancestors' pids. The verdicts are returned
# in scan order so a test asserts the whole plan.

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from services.agent_runner.page_server.page_plan import PAGE_SERVER_MODULE, PageKey

__all__ = [
    "Occupant",
    "OccupantAction",
    "OccupantVerdict",
    "owning_page",
    "port_of",
    "reclaim_plan",
]

_PORT = re.compile(r"--port (\d+)")


# meiva:note
# One process holding a page-server port: its pid, its home and the pids of its ancestors.
@dataclass(frozen=True)
class Occupant:
    port: int
    pid: int
    home: str | None
    ancestors: tuple[int, ...]


# meiva:note
# What the daemon does with one occupant.
class OccupantAction(Enum):
    KEEP = "keep"
    SKIP_FOREIGN = "skip-foreign"
    RECLAIM_WANTED = "reclaim-wanted"
    CLOSE_ORPHAN = "close-orphan"


# meiva:note
# The verdict for one occupant.
@dataclass(frozen=True)
class OccupantVerdict:
    occupant: Occupant
    action: OccupantAction


# meiva:note
# The port a page-server command line serves, or None when the process is not a page server.
def port_of(cmdline: Sequence[str]) -> int | None:
    rendered = " ".join(cmdline)
    if PAGE_SERVER_MODULE not in rendered:
        return None
    match = _PORT.search(rendered)
    return None if match is None else int(match.group(1))


# meiva:note
# The page whose live shell is the nearest ancestor of a process, or None for a process outside
# every supervised shell.
def owning_page(ancestors: Sequence[int], shell_pids: Mapping[int, PageKey]) -> PageKey | None:
    for pid in ancestors:
        owner = shell_pids.get(pid)
        if owner is not None:
            return owner
    return None


# meiva:note
# Keep servers inside a live page shell, skip those of another cluster home, and kill the
# detached rest: a wanted port is reclaimed, any other port is an orphan.
def reclaim_plan(
    occupants: Sequence[Occupant],
    shell_pids: Mapping[int, PageKey],
    wanted_ports: Collection[int],
    own_home: str,
) -> tuple[OccupantVerdict, ...]:
    verdicts: list[OccupantVerdict] = []
    for occupant in occupants:
        if owning_page(occupant.ancestors, shell_pids) is not None:
            action = OccupantAction.KEEP
        elif occupant.home != own_home:
            action = OccupantAction.SKIP_FOREIGN
        elif occupant.port in wanted_ports:
            action = OccupantAction.RECLAIM_WANTED
        else:
            action = OccupantAction.CLOSE_ORPHAN
        verdicts.append(OccupantVerdict(occupant, action))
    return tuple(verdicts)
