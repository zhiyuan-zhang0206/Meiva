"""Client of the pty-sessions service: what agent, schedule and page-server processes dial.

One short connection per request over the service's unix socket, so an agent or
agent host restarting simply dials again. It stays light (sockets and the
closure's data types): it runs inside every process that touches a shell.

Queries (`has_session`, `list_sessions`, `live_sessions`) read a service that is
not running as "no sessions", which is true: its sessions ended with it.
Mutating requests raise `ServiceUnavailableError` instead, after a bounded wait for a
service that is still coming up (a start races its own clients).
"""

from __future__ import annotations

import base64
import math
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from base.sessions.pty import protocol
from base.sessions.pty.closure import Outcome
from base.sessions.pty.paths import CAPTURE_MAX_LINES
from base.sessions.record import SessionRecord

# Long enough to cover the service's longest single-session op (a graceful kill
# waits up to its own bound before escalating); connecting stays instant.
REQUEST_TIMEOUT_S = 30.0


# meiva:note
# Where the client dials and how patiently; the wait default is the production value.
#
# `socket_path` is the service's unix socket (`base.sessions.pty.paths.service_socket_path`);
# `connect_wait_s` is how long a mutating request waits for a service that is not accepting yet.
@dataclass(frozen=True)
class PtyClientConfig:
    socket_path: Path
    connect_wait_s: float = 5.0

    # meiva:note
    # A wait `connect_attempts` refuses fails when the config is built, not on the first request.
    def __post_init__(self) -> None:
        connect_attempts(self.connect_wait_s)


_CONNECT_RETRY_S = 0.1


class ServiceUnavailableError(OSError):
    """The pty-sessions service did not answer: it is down, or wedged."""


class ServiceDownError(ServiceUnavailableError):
    """No pty-sessions service is listening: its socket is absent or refuses connections.

    Nothing can be alive in a service that is not there, which is what the queries
    below read it as.
    """


class ServiceError(RuntimeError):
    """The service refused or failed a request; `code` is its protocol code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionInfo:
    """One live session as `list` reports it."""

    name: str
    pid: int
    create_time: float
    starttime: int | None
    cmd: str
    cwd: str
    started_at: float
    generation: str | None
    initial_command: str | None = None

    def record(self) -> SessionRecord:
        return SessionRecord(
            self.pid,
            self.create_time,
            self.cmd,
            self.cwd,
            self.started_at,
            self.starttime,
            self.generation,
        )


# meiva:note
# A shell-session kill verdict; survivors name only known observed leftovers.
@dataclass(frozen=True)
class KillVerdict:
    mode: str
    interrupted: bool
    survivors: tuple[int, ...] = ()


def _is_json_object(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


# meiva:note
# How many times a connect is tried for a wait of `wait_s` seconds: the first attempt, then one more
# per fixed retry pause that fits in the wait. A pause is the unit instead of a deadline because
# an attempt on a unix socket is a single syscall, so the pauses are the whole wait and no clock
# is read to decide when to stop.
def connect_attempts(wait_s: float) -> int:
    if not math.isfinite(wait_s) or wait_s < 0:
        raise ValueError(f"connect wait must be a non-negative number of seconds: {wait_s!r}")
    return 1 + math.ceil(wait_s / _CONNECT_RETRY_S)


def _dial(path: str) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except BaseException:
        conn.close()
        raise
    return conn


def _connect(wait_s: float, socket_path: Path) -> socket.socket:
    path = str(socket_path)
    for _ in range(connect_attempts(wait_s) - 1):
        try:
            return _dial(path)
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(_CONNECT_RETRY_S)
    try:
        return _dial(path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ServiceDownError(f"no pty-sessions service is listening at {path}") from exc


# meiva:note
# Send one request and return the response's data.
#
# `socket_path` names the service's socket (a read-only scan that must not create
# the home passes one it computed from an explicit run directory).
#
# Raises:
#     ServiceDownError: no service listens (after waiting up to `wait` seconds for
#         one to accept).
#     ServiceUnavailableError: a service was dialed and did not answer.
#     ServiceError: the service refused or failed the request.
def request(
    method: str,
    *,
    socket_path: Path,
    timeout: float = REQUEST_TIMEOUT_S,
    wait: float = 0.0,
    **fields: object,
) -> dict[str, Any]:
    req_id = 1
    conn = _connect(wait, socket_path)
    with conn:
        conn.settimeout(timeout)
        try:
            conn.sendall(protocol.encode({"id": req_id, "method": method, **fields}))
            line = protocol.read_line(conn, limit=1 << 30)
        except (TimeoutError, OSError, ValueError) as exc:
            raise ServiceUnavailableError(f"pty-sessions {method} got no answer: {exc}") from exc
    if line is None:
        raise ServiceUnavailableError(
            f"pty-sessions closed the connection without answering {method}"
        )
    try:
        response = protocol.decode_object(line)
    except (ValueError, TypeError) as exc:
        raise ServiceUnavailableError(f"pty-sessions returned a malformed response: {exc}") from exc
    if response.get("id") != req_id:
        raise ServiceUnavailableError(
            f"pty-sessions answered request {req_id} with id {response.get('id')!r}"
        )
    if not response.get("ok"):
        raise ServiceError(int(response.get("code") or protocol.ERROR), str(response.get("error")))
    data = response.get("data")
    return data if _is_json_object(data) else {}


def has_session(name: str, *, socket_path: Path) -> bool:
    try:
        return bool(request("has", socket_path=socket_path, name=name)["alive"])
    except ServiceDownError:
        return False


# meiva:note
# The service mints one generation id per process and reports it on ping; a restarted service always answers a different one. An absent service still answers queries as 'no sessions', so a consumer that acts on absence reads the generation first and treats None as unknown.
# meiva:link base/sessions/pty/docs/pty_sessions/host-generation.meiva.okf.md
def host_generation(*, socket_path: Path) -> str | None:
    try:
        gen = request("ping", socket_path=socket_path).get("host_generation")
        return gen if isinstance(gen, str) else None
    except ServiceDownError:
        return None


def list_sessions(
    prefix: str = "", *, socket_path: Path, include_initial_command: bool = False
) -> list[SessionInfo]:
    """Every live session whose name starts with `prefix`, sorted by name."""
    try:
        fields: dict[str, Any] = {"prefix": prefix}
        if include_initial_command:
            fields["include_initial_command"] = True
        rows = request("list", socket_path=socket_path, **fields)["sessions"]
    except ServiceDownError:
        return []
    return [SessionInfo(**row) for row in rows]


def live_sessions(prefix: str = "", *, socket_path: Path) -> dict[str, SessionRecord]:
    """Every live session's record by name: the shape the page server reconciles over."""
    return {info.name: info.record() for info in list_sessions(prefix, socket_path=socket_path)}


def create_session(
    name: str, cwd: str, env: dict[str, str], cmd: str | None = None, *, config: PtyClientConfig
) -> bool:
    """Create the session; True when this call created it, False when it already lived.

    Raises:
        ServiceError: the service refused (its message names the reason).
        ServiceUnavailableError: no service answers.
    """
    fields: dict[str, Any] = {"name": name, "cwd": cwd, "env": env}
    if cmd:
        fields["cmd"] = cmd
    return bool(
        request("new", socket_path=config.socket_path, wait=config.connect_wait_s, **fields)[
            "created"
        ]
    )


def send(name: str, data: bytes, *, socket_path: Path) -> None:
    request("send", socket_path=socket_path, name=name, data=base64.b64encode(data).decode("ascii"))


def capture(name: str, lines: int, *, scrollback: bool, socket_path: Path) -> str:
    if not 1 <= lines <= CAPTURE_MAX_LINES:
        raise ValueError(f"capture lines out of range: {lines}")
    return str(
        request("capture", socket_path=socket_path, name=name, lines=lines, scrollback=scrollback)[
            "text"
        ]
    )


def resize(name: str, cols: int, rows: int, *, socket_path: Path) -> None:
    request("resize", socket_path=socket_path, name=name, cols=cols, rows=rows)


def kill(name: str, *, graceful: bool, socket_path: Path) -> KillVerdict:
    """End the session and report whether it cut running work short (idempotent).

    Without a service there is no session to end: a noop, like killing an absent one.
    """
    try:
        data = request("kill", socket_path=socket_path, name=name, graceful=graceful)
    except ServiceDownError:
        return KillVerdict("noop", interrupted=False)
    return KillVerdict(
        str(data["mode"]), bool(data["interrupted"]), tuple(data.get("survivors", ()))
    )


# meiva:note
# Close known shell sessions best effort; no descendant-absence proof.
def close_all(*, grace_s: float, kill_s: float, socket_path: Path) -> Outcome:
    data = request(
        "close_all",
        socket_path=socket_path,
        timeout=grace_s + 4 * kill_s + REQUEST_TIMEOUT_S,
        grace_s=grace_s,
        kill_s=kill_s,
    )
    return Outcome.from_wire(data)
