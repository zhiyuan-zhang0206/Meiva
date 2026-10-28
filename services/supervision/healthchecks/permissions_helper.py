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

import psutil

from base.daemon.health import DaemonProbe
from base.host.permissions_helper import client
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import peer_pid
from services.desktop.permissions_helper.launchd_job import (
    BoundedRunner,
    HelperJobState,
    LaunchdFacts,
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


# meiva:note
# The native parent of `root`, proven to be the process on the other end of `sock`.
def _helper_parent(sock: socket.socket, *, root: OwnedProcess) -> OwnedProcess:
    try:
        return _read_helper_parent(sock, root=root)
    except Exception as exc:
        raise ParentEvidenceError(f"helper parent evidence unavailable: {exc}") from exc


def _read_helper_parent(sock: socket.socket, *, root: OwnedProcess) -> OwnedProcess:
    parent = psutil.Process(root.pid).parent()
    if parent is None:
        raise ParentEvidenceError("root has no observable native parent")
    owner = OwnedProcess.capture(parent)
    if peer_pid(sock) != owner.pid or not root.live() or not owner.live():
        raise ParentEvidenceError("helper peer is not the captured root parent")
    return owner


def _parent_still_live(root: OwnedProcess, parent: OwnedProcess, reply_pid: int) -> bool:
    try:
        return (
            reply_pid == parent.pid
            and root.live()
            and parent.live()
            and psutil.Process(root.pid).ppid() == parent.pid
        )
    except psutil.Error as exc:
        raise ParentEvidenceError(f"cannot recheck helper parent: {exc}") from exc


# meiva:note
# Ping the real helper protocol with a watchdog-sized response timeout. `root` is the observing
# process; the endpoint answers only if it is root's native parent.
def ping(*, sock_path: Path, root: OwnedProcess) -> bool:
    sock = client.connect(str(sock_path))
    try:
        sock.settimeout(_PING_TIMEOUT_S)
        parent = _helper_parent(sock, root=root)
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
    if "pid" not in result or not _parent_still_live(root, parent, result["pid"]):
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


# meiva:note
# None when the helper answered pong; otherwise the greppable reason it did not. Missing parent
# evidence is not a failure of the helper, so it propagates.
def _read_ping_failure(*, sock_path: Path, root: OwnedProcess) -> str | None:
    try:
        answered = ping(sock_path=sock_path, root=root)
    except ParentEvidenceError:
        raise
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if answered else "pong was not true"


# meiva:note
# The probe verdict (pure): up when ping answered, else the launchd-classified down. `job_text` is
# the `launchctl print` dump read after a failed ping, None when launchd has no readable job.
def verdict(*, failure: str | None, job_text: str | None) -> DaemonProbe:
    if failure is None:
        return DaemonProbe.up("ping answered; helper serving")
    job = None if job_text is None else parse_job_state(job_text)
    classification = classify(ping_ok=False, job=job)
    return DaemonProbe.down(_detail(classification, job, failure))


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
    home: Path,
    facts: LaunchdFacts,
    run: BoundedRunner,
    sock_path: Path,
    root: OwnedProcess,
) -> DaemonProbe:
    """Detect and classify; inspection failures remain unavailable evidence."""
    if facts.platform != "darwin":
        return DaemonProbe.up("not macOS: no launchd-owned permissions helper")
    try:
        failure = _read_ping_failure(sock_path=sock_path, root=root)
        job_text = None if failure is None else read_helper_job(home=home, facts=facts, run=run)
    except Exception as exc:
        return DaemonProbe.unavailable(f"probe error: {type(exc).__name__}: {exc}")
    return verdict(failure=failure, job_text=job_text)
