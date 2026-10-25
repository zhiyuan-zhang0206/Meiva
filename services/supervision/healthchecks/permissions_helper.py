"""Read-only macOS parent-helper ping and launchd-state classification.

The root cannot restart its own permission ancestor. Failed episodes require an
external transition to settle custody before helper replacement.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

from base.daemon.health import DaemonProbe
from base.host.permissions_helper import client
from base.native_process.os_platform import is_macos
from base.native_process.ownership import OwnedProcess
from base.sessions.helperproc import helper_client
from services.desktop.permissions_helper.launchd_job import (
    HelperJobState,
    parse_job_state,
    read_helper_job,
)

_PING_TIMEOUT_S = 3.0
_PING_RESPONSE_LIMIT = 64 * 1024
# Classification vocabulary (the values carried by logs and event fields).
HEALTHY = "healthy"
LWCR_STUCK = "lwcr-stuck"
SPAWN_FAILED = "spawn-failed"
UNRESPONSIVE = "unresponsive"
ABSENT = "absent"


class ParentEvidenceError(RuntimeError):
    """The observer cannot bind the helper endpoint to the captured root parent."""


def _default_root_process() -> OwnedProcess | None:
    from base.native_process.root_control.client import root_process
    from base.paths import meiva_home

    return root_process(home=meiva_home())


def _default_parent_of(pid: int) -> psutil.Process | None:
    return psutil.Process(pid).parent()


def _default_peer_pid(sock: socket.socket) -> int | None:
    from base.native_process.root_control.client import peer_pid

    return peer_pid(sock)


def _default_sock_path() -> str:
    return str(helper_client().sock_path)


def helper_parent(
    sock: socket.socket,
    *,
    root: OwnedProcess | None = None,
    parent: OwnedProcess | None = None,
    peer_pid: int | None = None,
) -> tuple[OwnedProcess, OwnedProcess]:
    try:
        return _read_helper_parent(
            sock,
            root=root,
            parent=parent,
            peer_pid=peer_pid,
        )
    except Exception as exc:
        raise ParentEvidenceError(f"helper parent evidence unavailable: {exc}") from exc


def _read_helper_parent(
    sock: socket.socket,
    *,
    root: OwnedProcess | None = None,
    parent: OwnedProcess | None = None,
    peer_pid: int | None = None,
) -> tuple[OwnedProcess, OwnedProcess]:
    resolved_root = _default_root_process() if root is None else root
    if resolved_root is None:
        raise ParentEvidenceError("no captured root generation")
    if parent is None:
        parent_proc = _default_parent_of(resolved_root.pid)
        if parent_proc is None:
            raise ParentEvidenceError("root has no observable native parent")
        owner = OwnedProcess.capture(parent_proc)
    else:
        owner = parent
    resolved_peer_pid = _default_peer_pid(sock) if peer_pid is None else peer_pid
    if resolved_peer_pid != owner.pid or not resolved_root.live() or not owner.live():
        raise ParentEvidenceError("helper peer is not the captured root parent")
    return resolved_root, owner


def parent_still_live(root: OwnedProcess, parent: OwnedProcess, reply_pid: int) -> bool:
    try:
        return (
            reply_pid == parent.pid
            and root.live()
            and parent.live()
            and psutil.Process(root.pid).ppid() == parent.pid
        )
    except psutil.Error as exc:
        raise ParentEvidenceError(f"cannot recheck helper parent: {exc}") from exc


def ping(
    *,
    sock_path: Path | str | None = None,
    root: OwnedProcess | None = None,
    parent: OwnedProcess | None = None,
    peer_pid: int | None = None,
    parent_live: bool | None = None,
) -> bool:
    """Ping the real helper protocol with a watchdog-sized response timeout."""
    resolved_path = _default_sock_path() if sock_path is None else str(sock_path)
    sock = client.connect(resolved_path)
    try:
        sock.settimeout(_PING_TIMEOUT_S)
        resolved_root, resolved_parent = helper_parent(
            sock,
            root=root,
            parent=parent,
            peer_pid=peer_pid,
        )
        sock.sendall((json.dumps({"id": 0, "method": "ping"}) + "\n").encode())
        reply = bytearray()
        while not reply.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply.extend(chunk)
            if len(reply) > _PING_RESPONSE_LIMIT:
                raise client.PermissionsHelperError(
                    "permissions helper ping response exceeded line limit"
                )
    finally:
        sock.close()

    result: client.PingResult = client.parse_reply(bytes(reply), "ping")
    still_live = (
        parent_still_live(resolved_root, resolved_parent, result["pid"])
        if parent_live is None and "pid" in result
        else bool(parent_live) and result.get("pid") == resolved_parent.pid
    )
    if "pid" not in result or not still_live:
        raise ParentEvidenceError("helper reply lost captured parent identity")
    return result["pong"] is True


@dataclass(frozen=True, slots=True)
class _Observation:
    """One round's view of the helper: ping result plus launchd classification."""

    classification: str
    detail: str
    job: HelperJobState | None = None


def classify(*, ping_ok: bool, job: HelperJobState | None) -> str:
    """The LWCR truth table (pure): one failure class per observation.

    ``job`` is None when launchd has no readable job. The LWCR class needs the
    spawn-failed ``job state`` plus one LWCR signal — the ``needs LWCR update``
    marker or the 78 exit code; a bare 78 without spawn-failed is not one.
    """
    if ping_ok:
        return HEALTHY
    if job is None:
        return ABSENT
    if job.job_state == "spawn failed":
        exit_code = job.last_exit_code or ""
        if job.needs_lwcr_update or exit_code.startswith("78"):
            return LWCR_STUCK
        return SPAWN_FAILED
    return UNRESPONSIVE


def _detail(classification: str, job: HelperJobState | None, ping_failure: str | None) -> str:
    """One greppable line: the classification plus the launchd facts behind it."""
    parts = [classification]
    if job is not None:
        if job.job_state is not None:
            parts.append(f"job state={job.job_state}")
        if job.last_exit_code is not None:
            parts.append(f"last exit={job.last_exit_code}")
        if job.needs_lwcr_update:
            parts.append("needs LWCR update")
        if job.btm_uuid is not None:
            parts.append(f"BTM uuid={job.btm_uuid}")
        if job.runs is not None:
            parts.append(f"runs={job.runs}")
    if ping_failure is not None:
        parts.append(ping_failure)
    return "; ".join(parts)


_UNSET_JOB: Final = object()


def _observe(
    *,
    ping_ok: bool | None = None,
    ping_error: Exception | None = None,
    job_text: str | object | None = _UNSET_JOB,
) -> _Observation:
    """Ping the helper; on failure read and classify the launchd job (total)."""
    if ping_error is not None:
        if isinstance(ping_error, ParentEvidenceError):
            raise ping_error
        resolved_ping_ok = False
        ping_failure: str | None = f"{type(ping_error).__name__}: {ping_error}"
    elif ping_ok is not None:
        resolved_ping_ok = ping_ok
        ping_failure = None if resolved_ping_ok else "pong was not true"
    else:
        try:
            resolved_ping_ok = ping()
        except ParentEvidenceError:
            raise
        except Exception as exc:
            resolved_ping_ok = False
            ping_failure = f"{type(exc).__name__}: {exc}"
        else:
            ping_failure = None if resolved_ping_ok else "pong was not true"
    if resolved_ping_ok:
        return _Observation(HEALTHY, "ping answered")
    job = None
    text = (
        read_helper_job()
        if job_text is _UNSET_JOB
        else job_text
        if isinstance(job_text, str)
        else None
    )
    if text is not None:
        job = parse_job_state(text)
    classification = classify(ping_ok=False, job=job)
    return _Observation(classification, _detail(classification, job, ping_failure), job)


def episode_reporter() -> Callable[[DaemonProbe], None]:
    """Bind one diagnostic's reporting memory; a healthy sample rearms its episode."""
    reported_unhealthy = False

    def report(result: DaemonProbe) -> None:
        nonlocal reported_unhealthy
        if result.alive:
            reported_unhealthy = False
        elif not reported_unhealthy:
            reported_unhealthy = True
            _emit_unhealthy(_Observation(result.detail.split(";", 1)[0], result.detail))

    return report


def _job_fields(observation: _Observation) -> dict[str, object]:
    """The launchd facts as structured event fields (None when unread)."""
    job = observation.job
    return {
        "job_state": None if job is None else job.job_state,
        "last_exit_code": None if job is None else job.last_exit_code,
        "needs_lwcr_update": None if job is None else job.needs_lwcr_update,
        "btm_uuid": None if job is None else job.btm_uuid,
    }


def _emit_unhealthy(observation: _Observation) -> None:
    """One registered event per episode — the report the operator surfaces inherit."""
    from base.log import logger

    logger.error(
        "[permissions-helper healthcheck] helper unhealthy ({classification})",
        event="permissions_helper_unhealthy",
        classification=observation.classification,
        detail=observation.detail,
        **_job_fields(observation),
    )


def probe(
    *,
    macos: bool | None = None,
    ping_ok: bool | None = None,
    ping_error: Exception | None = None,
    job_text: str | object | None = _UNSET_JOB,
    job_error: Exception | None = None,
) -> DaemonProbe:
    """Detect and classify; inspection failures remain unavailable evidence."""
    if not (is_macos() if macos is None else macos):
        return DaemonProbe.up("not macOS: no launchd-owned permissions helper")
    if job_error is not None and not ping_ok:
        exc = ping_error if isinstance(ping_error, ParentEvidenceError) else job_error
        return DaemonProbe.unavailable(f"probe error: {type(exc).__name__}: {exc}")
    try:
        observation = _observe(
            ping_ok=ping_ok,
            ping_error=ping_error,
            job_text=job_text,
        )
    except Exception as exc:
        return DaemonProbe.unavailable(f"probe error: {type(exc).__name__}: {exc}")
    if observation.classification == HEALTHY:
        return DaemonProbe.up("ping answered; helper serving")
    return DaemonProbe.down(observation.detail)
