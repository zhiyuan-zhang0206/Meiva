# meiva:note
# What the MCP daemon decides before it binds its socket, as values: which socket the launch arguments name, whether a live occupant forbids starting, and which other daemon processes are ghosts of this unit that must be reaped first. The shell in `_guards.py` reads the process table and the socket and kills what these functions select.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DAEMON_MODULE = "meiva.mcps._daemon"
USAGE = "Usage: python -m meiva.mcps._daemon [socket_path]"


# meiva:note
# The socket a launch binds: none or one argument is the shared per-machine socket or that path (tests, migration); two or more arguments are no launch, so None.
def socket_for_args(args: Sequence[str], default_socket: str) -> str | None:
    if len(args) > 1:
        return None
    return args[0] if args else default_socket


# meiva:note
# Why a daemon must not start over `socket_path`, or None when it may. A live occupant answers the watchdog's ping, so a stray launch exits instead of stealing the socket from the serving daemon (Task #1142); a dead file is safe to replace.
def refusal_to_start(socket_path: str, *, occupied: bool) -> str | None:
    if occupied:
        return f"MCP daemon already serving {socket_path} — refusing to start over it; exiting"
    return None


# meiva:note
# True when `cmdline` is an actual `python -m meiva.mcps._daemon` launch. A substring match is not enough: the session backend wraps the launch in `bash -lc '... python -m meiva.mcps._daemon'`, whose single argv element contains the module name but is no daemon, and killing that wrapper orphans the live daemon. The module name must be its own argv element right after `-m`, which also matches a daemon started through `exec`.
def is_daemon_cmdline(cmdline: Sequence[str]) -> bool:
    return any(
        part == DAEMON_MODULE and i > 0 and cmdline[i - 1] == "-m" for i, part in enumerate(cmdline)
    )


# meiva:note
# What the reaper reads of one process: its pid and argv always; its working directory and `MEIVA_HOME` only when the argv is a daemon launch (empty strings otherwise), since only a daemon's unit is ever compared.
@dataclass(frozen=True)
class ProcessFacts:
    pid: int
    cmdline: tuple[str, ...]
    cwd: str
    home: str


# meiva:note
# The pids of the ghost daemons of this unit: every other process whose argv is a daemon launch and that runs from this unit's project `root` or with this unit's `home`. A co-located daemon of another cluster (same binary, other home, other root) is never selected.
def stale_daemon_pids(
    processes: Iterable[ProcessFacts], *, me: int, home: str, root: str
) -> list[int]:
    return [
        process.pid
        for process in processes
        if process.pid != me
        and is_daemon_cmdline(process.cmdline)
        and ((bool(root) and process.cwd == root) or process.home == home)
    ]
