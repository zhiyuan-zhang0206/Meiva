# meiva:note
# The effects behind the MCP daemon's socket ownership guards (Task #1142). The ghost-storm root cause: a respawn storm (restarter or rollout wave relaunching the daemon while the previous detached process is unreachable by session kill) leaves ghost daemons listening on an unlinked inode. Two hazards follow, both guarded here: a new daemon unlinked the socket file unconditionally before binding, severing the LIVE socket of the serving daemon (it became a ghost); and a killed ghost's exit cleanup unlinked the socket file again, by then someone else's. So a start refuses a live socket, reaps this unit's stale daemons before binding, and unlinks only the file it bound itself. What each guard decides lives in `_startup.py`; this module probes the socket, reads the process table, kills, and unlinks.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
from __future__ import annotations

import json
import os
import socket
from contextlib import suppress
from pathlib import Path

import psutil
from loguru import logger

from meiva.mcps._startup import ProcessFacts, is_daemon_cmdline, stale_daemon_pids


# meiva:note
# True when a daemon answers a ping over `socket_path`. Mirrors the healthcheck's probe (connect, ping, reply). A socket that connects but never answers counts as NOT live: a half-dead occupant must be replaceable, not shielded forever by a connect that succeeds.
def socket_is_live(socket_path: str, *, timeout_seconds: float) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps({"id": 0, "method": "ping"}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0])
        return bool(resp.get("ok"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        return False
    finally:
        sock.close()


# meiva:note
# Unlink `socket_path` only while it is still the inode this daemon bound: a ghost's exit must not sever the live socket of a later occupant, whose file replaced ours since we bound it.
def unlink_own_socket(socket_path: str, self_ino: int) -> None:
    with suppress(OSError):
        if Path(socket_path).stat().st_ino == self_ino:
            Path(socket_path).unlink()


# meiva:note
# One process as the reaper sees it, or None when it cannot be read. One unreadable process skips, never the reap: psutil 7.2.2 on macOS raised SystemError reading some cmdlines, and as an attrs read inside `process_iter` it aborted the whole pass, so ghosts of earlier respawn storms survived (task #4964). The working directory and environment are read only for a daemon-shaped argv.
def process_facts(proc: psutil.Process) -> ProcessFacts | None:
    try:
        cmdline = tuple(proc.cmdline() or [])
        if not is_daemon_cmdline(cmdline):
            return ProcessFacts(proc.pid, cmdline, "", "")
        return ProcessFacts(proc.pid, cmdline, proc.cwd(), proc.environ().get("MEIVA_HOME", ""))
    except (psutil.Error, OSError, SystemError):
        return None


# meiva:note
# Kill every OTHER mcp-daemon process belonging to the unit named by `root` (its project root) and `home` (its `MEIVA_HOME`), and return how many died. A fresh bind must be the only listener; ghosts from earlier respawn storms are reaped first. Only a process whose argv IS the daemon launch qualifies (see `is_daemon_cmdline`): the session backend's `bash -lc` wrapper is never reaped, since killing it would orphan the live daemon and fake a dead session (#1199).
def reap_stale_daemons(*, root: str, home: str) -> int:
    seen: dict[int, tuple[ProcessFacts, psutil.Process]] = {}
    for proc in psutil.process_iter():
        facts = process_facts(proc)
        if facts is not None:
            seen[facts.pid] = (facts, proc)
    pids = stale_daemon_pids((f for f, _ in seen.values()), me=os.getpid(), home=home, root=root)
    reaped = 0
    for pid in pids:
        try:
            seen[pid][1].kill()
        except (psutil.Error, OSError, SystemError):
            continue
        reaped += 1
    if reaped:
        logger.warning("reaped {} stale mcp-daemon process(es) before binding", reaped)
    return reaped
