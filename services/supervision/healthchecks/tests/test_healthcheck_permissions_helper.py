"""Watchdog probe and launchd classifier for the launchd-owned macOS helper."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base.daemon.health import DaemonProbe
from base.host.permissions_helper import client
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.desktop.permissions_helper.launchd_job import LaunchdFacts
from services.supervision.healthchecks import permissions_helper as hc


def _refuse_launchctl(
    cmd: Sequence[str], /, *, timeout: float, capture_output: bool = True
) -> subprocess.CompletedProcess[bytes]:
    del timeout, capture_output
    raise AssertionError(f"this test must not run {list(cmd)}")


# meiva:note
# `hc.probe` over what it reads of the world: a home, the account's launchd facts and a runner.
# The cases below hand the probe the job text they want classified, so the runner is never asked.
def _probe(**changes: Any) -> DaemonProbe:
    return hc.probe(
        home=Path("/unused/home"),
        facts=LaunchdFacts(uid=501, platform="darwin", user_home=Path("/unused/user")),
        run=_refuse_launchctl,
        **changes,
    )


# Real `launchctl print` excerpts (F5 run-09/10c/11 evidence), shortened. The
# stuck shape deliberately keeps BOTH the top-level `state = spawn scheduled`
# line and the `job state = spawn failed` line: the classifier must read the
# latter only (PR #2500 review; the top-level `state` never carries it).
_STUCK_JOB = """gui/501/com.meiva.test.f5-lwcr-stub = {
	active count = 0
	managed_by = com.apple.xpc.ServiceManagement
	state = spawn scheduled
	program = /Users/example/Applications/F5LWCRStub.app/Contents/MacOS/F5LWCRStub
	BTM uuid = 2F5F25DF-BFD3-474F-B9DF-8EBF83A685AA
	runs = 26
	last exit code = 78: EX_CONFIG
	job state = spawn failed
	properties = partial import | keepalive | runatload | needs LWCR update | has LWCR
}"""

# The lag window: `spawn failed` is already visible, the LWCR marker too, but
# launchd has not recorded the exit code yet — marker-first still classifies.
_STUCK_MARKER_ONLY = """gui/501/com.meiva.test.f5-lwcr-stub = {
	state = spawn scheduled
	job state = spawn failed
	properties = keepalive | needs LWCR update | has LWCR
}"""

# The mirror lag: the 78 exit is recorded but no LWCR marker shows in the
# properties — the exit code alone already identifies the stuck class.
_STUCK_78_NO_MARKER = """gui/501/com.meiva.test.f5-lwcr-stub = {
	state = spawn scheduled
	job state = spawn failed
	properties = keepalive
	last exit code = 78: EX_CONFIG
}"""

# Spawn failed without any LWCR mark: the next round re-classifies.
_SPAWN_FAILED_BARE = """gui/501/com.meiva.test.f5-lwcr-stub = {
	state = spawn scheduled
	last exit code = 1: EPERM
	job state = spawn failed
	properties = keepalive
}"""

# Exit 78 without spawn-failed: not an LWCR verdict (78 is a generic spawn
# failure code, e.g. a missing app).
_EXIT_78_ONLY = """gui/501/com.meiva.test.f5-lwcr-stub = {
	state = running
	last exit code = 78: EX_CONFIG
}"""

_RUNNING_JOB = """gui/501/com.meiva.test.f5-lwcr-stub = {
	state = running
	pid = 4242
}"""


class _Recorder:
    """The structured log calls so far, as their field dictionaries (read from `loguru_records`)."""

    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [{"message": r["message"], **r["extra"]} for r in self._records]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


def test_ping_uses_short_timeout_and_helper_wire_protocol(tmp_path: Path) -> None:
    received: list[bytes] = []
    response = (
        b'{"id":0,"ok":true,"result":{"pong":true,"pid":42,'
        b'"preflight_screen":true,"ax_trusted":true}}\n'
    )
    with tempfile.TemporaryDirectory(prefix="meiva-helper-", dir="/tmp") as directory:
        socket_path = f"{directory}/helper.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        server.listen(1)

        def _serve() -> None:
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(4096))
                conn.sendall(response)

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            root = cast(OwnedProcess, SimpleNamespace(pid=10, live=lambda: True))
            parent = cast(OwnedProcess, SimpleNamespace(pid=42, live=lambda: True))
            assert hc.ping(
                home=tmp_path,
                sock_path=socket_path,
                root=root,
                parent=parent,
                peer_pid=42,
                parent_live=True,
            )
        finally:
            server.close()
            worker.join(timeout=2)

    assert len(received) == 1
    assert json.loads(received[0]) == {"id": 0, "method": "ping"}


# -- parsing and the LWCR truth table (pure) ----------------------------------


def test_parse_reads_job_state_not_top_level_state() -> None:
    facts = hc.parse_job_state(_STUCK_JOB)
    assert facts.job_state == "spawn failed"
    assert facts.last_exit_code == "78: EX_CONFIG"
    assert facts.needs_lwcr_update is True
    assert facts.btm_uuid == "2F5F25DF-BFD3-474F-B9DF-8EBF83A685AA"
    assert facts.runs == 26


def test_parse_tolerates_a_missing_exit_code_and_empty_text() -> None:
    facts = hc.parse_job_state(_STUCK_MARKER_ONLY)
    assert facts.job_state == "spawn failed"
    assert facts.last_exit_code is None
    empty = hc.parse_job_state("")
    assert empty.job_state is None and empty.btm_uuid is None and empty.runs is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_STUCK_JOB, hc.LWCR_STUCK),  # spawn failed + 78 + marker
        (_STUCK_MARKER_ONLY, hc.LWCR_STUCK),  # spawn failed + marker, exit lagging
        (_STUCK_78_NO_MARKER, hc.LWCR_STUCK),  # spawn failed + 78, marker lagging
        (_SPAWN_FAILED_BARE, hc.SPAWN_FAILED),  # spawn failed, no LWCR mark
        (_EXIT_78_ONLY, hc.UNRESPONSIVE),  # 78 without spawn-failed is not LWCR
        (_RUNNING_JOB, hc.UNRESPONSIVE),  # running but not answering ping
        (None, hc.ABSENT),  # no readable job
    ],
)
def test_classification_truth_table(text: str | None, expected: str) -> None:
    job = None if text is None else hc.parse_job_state(text)
    assert hc.classify(ping_ok=False, job=job) == expected


def test_ping_alive_short_circuits_to_healthy() -> None:
    assert hc.classify(ping_ok=True, job=hc.parse_job_state(_STUCK_JOB)) == hc.HEALTHY


# -- the watchdog round (era 1) ------------------------------------------------


def test_reporting_is_episode_gated_without_repair(recorder: _Recorder) -> None:
    from base.daemon.health import DaemonProbe

    report = hc.episode_reporter()
    bad = DaemonProbe.down("lwcr-stuck; needs LWCR update")
    report(bad)
    report(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 1
    report(DaemonProbe.up("ping answered"))
    report(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 2
    assert not hasattr(hc, "repair_unresponsive_helper")
    assert not hasattr(hc, "main")


def test_probe_alive_and_classified_down() -> None:
    assert _probe(macos=True, ping_ok=True).alive

    verdict = _probe(
        macos=True,
        ping_error=client.PermissionsHelperError("socket unavailable"),
        job_text=_STUCK_JOB,
    )
    assert not verdict.alive
    assert "lwcr-stuck" in verdict.detail
    assert "needs LWCR update" in verdict.detail

    assert _probe(
        macos=True,
        ping_error=client.PermissionsHelperError("socket unavailable"),
        job_text=None,
    ).detail.startswith(hc.ABSENT)


def test_probe_is_total_and_non_macos_is_up() -> None:
    assert _probe(macos=False).alive

    verdict = _probe(
        macos=True,
        ping_error=client.PermissionsHelperError("socket unavailable"),
        job_error=RuntimeError("boom"),
    )  # never raises
    assert not verdict.alive
    assert "probe error" in verdict.detail


def test_ping_dials_the_socket_of_the_home_it_is_given(tmp_path: Path) -> None:
    from base.sessions.helperproc import helper_client

    socket_path = str(helper_client(home=tmp_path).sock_path)

    with pytest.raises(client.PermissionsHelperError, match=re.escape(socket_path)):
        hc.ping(home=tmp_path)


def test_unverifiable_helper_parent_never_reports_protocol_health() -> None:
    assert (
        _probe(
            macos=True,
            ping_error=hc.ParentEvidenceError("root parent is not observable"),
            job_text=_RUNNING_JOB,
        ).verdict.value
        == "unavailable"
    )


def test_connected_helper_peer_must_be_root_native_parent(tmp_path: Path) -> None:
    root = cast(OwnedProcess, SimpleNamespace(pid=10, live=lambda: True))
    parent = cast(OwnedProcess, SimpleNamespace(pid=20, live=lambda: True))

    with pytest.raises(hc.ParentEvidenceError, match="not the captured root parent"):
        hc.helper_parent(
            cast(socket.socket, object()),
            home=tmp_path,
            root=root,
            parent=parent,
            peer_pid=30,
        )


def test_episode_reporting_memory_is_local_to_each_diagnostic(recorder: _Recorder) -> None:
    from base.daemon.health import DaemonProbe

    first = hc.episode_reporter()
    second = hc.episode_reporter()
    bad = DaemonProbe.down("spawn-failed; helper unavailable")
    first(bad)
    second(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 2
    first(DaemonProbe.up("ping answered"))
    first(bad)
    second(bad)
    assert len(recorder.events("permissions_helper_unhealthy")) == 3
