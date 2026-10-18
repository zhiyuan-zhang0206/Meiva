"""A blocking client for the K1 control plane.

The server (`services.supervision.meiva_root.server`) is asyncio; this client is a plain
blocking Unix socket — call sites (tooling, gates, tests) are
synchronous, and one request/response pair per connection is all the protocol
needs. Transport failures raise RootClientError; business
failures come back as a `{"ok": false, ...}` response for the caller to read.
"""

from __future__ import annotations

import math
import os
import re
import socket
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import psutil

from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import (
    MAX_MESSAGE_BYTES,
    CallerPayload,
    ProtocolError,
    ResponsePayload,
    encode,
    parse_response,
    parse_status_response,
)

_STATE_VERBS = frozenset({"up", "down", "force-down", "restart", "shutdown"})

_DEFAULT_TIMEOUT_S = 30.0

# meiva:note
# Audit metadata, not a place to leak one: argv is bounded and sensitive values
# are replaced, and environment *values* never leave the process.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
_ARGV_ITEMS = 32
_ARG_CHARS = 200
_REDACTED = "<redacted>"
_SENSITIVE_VALUE_FLAGS = frozenset({"-p", "--password", "--passwd", "--token", "--secret", "--key"})
_SENSITIVE_FLAG = re.compile(r"^--?[a-z0-9-]*(token|secret|password|passwd|key|credential)", re.I)
_SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL|BEARER)", re.I)


class RootClientError(RuntimeError):
    """The root supervisor could not be reached, or answered with garbage."""


def native_identity(value: object) -> OwnedProcess:
    """Validate a persisted native birth from a root status row."""
    if not isinstance(value, dict):
        raise RootClientError("root status omitted native identity")
    row = cast("dict[str, object]", value)
    pid, birth, starttime = row.get("pid"), row.get("create_time"), row.get("starttime")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(birth, bool)
        or not isinstance(birth, (int, float))
        or birth <= 0
        or not math.isfinite(birth)
        or (
            starttime is not None
            and (isinstance(starttime, bool) or not isinstance(starttime, int) or starttime < 0)
        )
    ):
        raise RootClientError("root status lacks a captured native birth")
    return OwnedProcess(pid, float(birth), starttime)


def peer_pid(sock: socket.socket) -> int:
    """Kernel-reported peer; a JSON PID cannot authenticate a local socket."""
    if sys.platform == "darwin":
        return sock.getsockopt(0, 2)  # SOL_LOCAL / LOCAL_PEERPID
    if sys.platform == "linux":
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    raise RootClientError("native Unix peer identity is unavailable on this platform")


def validate_status_peer(raw: bytes, peer: OwnedProcess) -> None:
    response = parse_response(raw)
    if not response["ok"]:
        return
    body = response.get("result")
    if not isinstance(body, dict):
        raise RootClientError("root status omitted its native identity")
    root = native_identity(cast("dict[str, object]", body).get("root"))
    if sys.platform == "linux" and (root.starttime is None or peer.starttime is None):
        raise RootClientError("exact native IPC peer requires observed Linux start ticks")
    if not root.same_birth(peer) or not peer.live():
        raise RootClientError("root status does not name the exact native IPC peer")


def root_process(*, home: Path, timeout: float = 2.0) -> OwnedProcess | None:
    """Read the captured native birth of the root of `home`; IPC failure remains unknown."""
    from base.paths import root_run_dir

    response = RootClient(root_run_dir(home) / "meiva-root.sock", timeout=timeout).status()
    raw = response.get("result")
    if response["ok"] is not True or not isinstance(raw, dict):
        raise RootClientError("root refused its native status")
    body = cast("dict[str, object]", raw)
    root = native_identity(body.get("root"))
    return root if root.live() else None


# meiva:note
# The caller block is what makes a lifecycle request attributable after the fact:
# pid/ppid/uid/cwd/argv name the local process, `machine` names the host (so two
# machines cannot be confused, as they were on 2026-10-15), and `env_keys` lists
# only the *names* of credential-shaped variables that were set.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def caller_identity() -> CallerPayload:
    return CallerPayload(
        pid=os.getpid(),
        ppid=os.getppid(),
        uid=os.getuid(),
        cwd=_bounded(str(Path.cwd())),
        argv=redacted_argv(list(sys.argv)),
        agent_id=os.environ.get("MEIVA_AGENT_ID") or None,
        machine=_machine_name(),
        operation_id=_operation_id(),
        env_keys=sensitive_env_keys(os.environ),
    )


# meiva:note
# Names of credential-shaped variables, never their values.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def sensitive_env_keys(environ: Mapping[str, str]) -> list[str]:
    return sorted(name for name in environ if _SENSITIVE_ENV.search(name))


def _bounded(text: str) -> str:
    return text[:_ARG_CHARS]


def redacted_argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    redact_next = False
    for raw in argv[: _ARGV_ITEMS * 2]:
        arg = _bounded(raw)
        if redact_next:
            out.append(_REDACTED)
            redact_next = False
            continue
        if arg in _SENSITIVE_VALUE_FLAGS:
            out.append(arg)
            redact_next = True
            continue
        if _SENSITIVE_FLAG.match(arg):
            head, separator, _value = arg.partition("=")
            out.append(f"{head}={_REDACTED}" if separator else arg)
            redact_next = not separator
            continue
        out.append(arg)
    return out[:_ARGV_ITEMS]


# meiva:note
# The host this request came from. The kernel hostname is used rather than the
# configured cluster name: resolving that one loads base.config, which already
# reaches this layer and would close an import cycle.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def _machine_name() -> str:
    return socket.gethostname().split(".")[0]


# meiva:note
# The operation an outer lifecycle verb belongs to, when the launcher exported
# one. Reading run/lifecycle-op.json here would make native_process depend on
# base.deploy, which already reaches this layer.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def _operation_id() -> str | None:
    return os.environ.get("MEIVA_OPERATION_ID") or None


class RootClient:
    """One blocking call per method, one connection per call."""

    def __init__(self, socket_path: Path, *, timeout: float = _DEFAULT_TIMEOUT_S) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    def call(self, verb: str, name: str | None = None) -> ResponsePayload:
        """Send one verb and return the parsed response."""
        request: dict[str, object] = {"verb": verb}
        if name is not None:
            request["name"] = name
        if verb in _STATE_VERBS:
            request["caller"] = caller_identity()
        raw = self._roundtrip(encode(request), status=verb == "status")
        try:
            return parse_status_response(raw) if verb == "status" else parse_response(raw)
        except ProtocolError as exc:
            raise RootClientError(f"malformed response from root supervisor: {exc}") from exc

    def up(self, name: str) -> ResponsePayload:
        """Bring `name` and its subtree up."""
        return self.call("up", name)

    def down(self, name: str) -> ResponsePayload:
        """Take `name` and its subtree down."""
        return self.call("down", name)

    def force_down(self, name: str) -> ResponsePayload:
        """Stop an explicitly authorized execution domain, with bounded escalation."""
        return self.call("force-down", name)

    def restart(self, name: str) -> ResponsePayload:
        """Stop then replace `name` and its subtree."""
        return self.call("restart", name)

    def status(self) -> ResponsePayload:
        """Read the tree snapshot."""
        return self.call("status")

    def shutdown(self) -> ResponsePayload:
        """Ask the root daemon to close its tree and exit gracefully."""
        return self.call("shutdown")

    def resource(self, operation: str, payload: dict[str, object]) -> ResponsePayload:
        """Request an explicitly registered deployment resource operation."""
        try:
            return parse_response(
                self._roundtrip(
                    encode(
                        {
                            "verb": "resource",
                            "name": operation,
                            "payload": payload,
                        }
                    )
                )
            )
        except ProtocolError as exc:
            raise RootClientError(f"malformed resource response: {exc}") from exc

    def _roundtrip(self, payload: bytes, *, status: bool = False) -> bytes:
        """One connect/send/read cycle; OSErrors become RootClientError."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(str(self._socket_path))
                peer = OwnedProcess.capture(psutil.Process(peer_pid(sock))) if status else None
                sock.sendall(payload)
                raw = _read_line(sock)
                if peer is not None:
                    validate_status_peer(raw, peer)
                return raw
        except (OSError, psutil.Error, ProtocolError, ValueError) as exc:
            raise RootClientError(
                f"root supervisor unreachable at {self._socket_path}: {exc}"
            ) from exc


def _read_line(sock: socket.socket) -> bytes:
    """Read one newline-terminated message, capped at the wire limit."""
    buffer = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            if not buffer:
                raise RootClientError("connection closed before any response")
            raise RootClientError("connection closed mid-response")
        index = chunk.find(b"\n")
        if index != -1:
            buffer.extend(chunk[:index])
            return bytes(buffer)
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise RootClientError(f"response exceeded the {MAX_MESSAGE_BYTES}-byte message cap")
