"""Watchdog probe and launchd classifier for the launchd-owned macOS helper."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from base.daemon.health import DaemonProbe
from base.host.permissions_helper import client
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.desktop.permissions_helper.launchd_job import LaunchdFacts, helper_job_label
from services.supervision.healthchecks import permissions_helper as hc
from services.supervision.healthchecks.tests.helper_endpoint import (
    live_root,
    serving,
    socket_dir,
    under_other_parent,
    wire_reply,
)

_DARWIN = LaunchdFacts(uid=7731, platform="darwin", user_home=Path("/unused/user"))


# meiva:note
# The account's `launchctl`, as the probe runs it: answers a `print` with the dump it was given (or
# raises what it was given) and records every command, so a case reads which job was asked for and
# whether launchctl was consulted at all.
class _Launchctl:
    def __init__(self, dump: str | None = None, *, error: Exception | None = None) -> None:
        self._dump = dump
        self._error = error
        self.commands: list[list[str]] = []

    def __call__(
        self, cmd: Sequence[str], /, *, timeout: float, capture_output: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout, capture_output
        self.commands.append(list(cmd))
        if self._error is not None:
            raise self._error
        if self._dump is None:
            return subprocess.CompletedProcess(cmd, 113, b"", b"Could not find service")
        return subprocess.CompletedProcess(cmd, 0, self._dump.encode(), b"")


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


def test_ping_speaks_the_wire_protocol_to_the_native_parent_of_the_root() -> None:
    with socket_dir() as directory, live_root() as root:
        sock_path = directory / "helper.sock"
        with serving(sock_path, wire_reply(os.getpid())) as received:
            assert hc.ping(sock_path=sock_path, root=root)
        assert len(received) == 1
        assert json.loads(received[0]) == {"id": 0, "method": "ping"}


def test_ping_reports_a_pong_that_is_not_true() -> None:
    with socket_dir() as directory, live_root() as root:
        sock_path = directory / "helper.sock"
        with serving(sock_path, wire_reply(os.getpid(), pong=False)):
            assert hc.ping(sock_path=sock_path, root=root) is False


@pytest.mark.parametrize("answering_pid", [None, 1])
def test_ping_refuses_a_reply_that_does_not_name_the_root_parent(answering_pid: int | None) -> None:
    with socket_dir() as directory, live_root() as root:
        sock_path = directory / "helper.sock"
        with (
            serving(sock_path, wire_reply(answering_pid)),
            pytest.raises(hc.ParentEvidenceError, match="lost captured parent identity"),
        ):
            hc.ping(sock_path=sock_path, root=root)


def test_ping_refuses_a_peer_that_is_not_the_native_parent_of_the_root() -> None:
    with socket_dir() as directory, under_other_parent() as root:
        sock_path = directory / "helper.sock"
        with (
            serving(sock_path, wire_reply(os.getpid())) as received,
            pytest.raises(hc.ParentEvidenceError, match="not the captured root parent"),
        ):
            hc.ping(sock_path=sock_path, root=root)
        assert all(b"ping" not in request for request in received)


def test_ping_refuses_a_root_that_no_longer_exists() -> None:
    with socket_dir() as directory:
        with live_root() as root:
            pass
        sock_path = directory / "helper.sock"
        with (
            serving(sock_path, wire_reply(os.getpid())),
            pytest.raises(hc.ParentEvidenceError, match="evidence unavailable"),
        ):
            hc.ping(sock_path=sock_path, root=root)


def test_ping_dials_the_socket_it_is_given() -> None:
    with socket_dir() as directory:
        sock_path = directory / "nobody-listens.sock"
        root = OwnedProcess(pid=os.getpid(), birth=1.0, starttime=None)
        with pytest.raises(client.PermissionsHelperError, match=re.escape(str(sock_path))):
            hc.ping(sock_path=sock_path, root=root)


@pytest.mark.parametrize(
    ("job_text", "classification"),
    [
        (_STUCK_JOB, hc.LWCR_STUCK),
        (_SPAWN_FAILED_BARE, hc.SPAWN_FAILED),
        (_RUNNING_JOB, hc.UNRESPONSIVE),
        (None, hc.ABSENT),
    ],
)
def test_a_failed_ping_is_down_with_the_launchd_classification(
    job_text: str | None, classification: str
) -> None:
    result = hc.verdict(failure="PermissionsHelperError: socket unavailable", job_text=job_text)
    assert not result.alive
    assert result.detail.startswith(classification)
    assert "PermissionsHelperError: socket unavailable" in result.detail


def test_a_failed_ping_names_the_launchd_facts_behind_the_class() -> None:
    detail = hc.verdict(failure="pong was not true", job_text=_STUCK_JOB).detail
    for fact in ("job state=spawn failed", "last exit=78", "needs LWCR update", "runs=26"):
        assert fact in detail


def test_an_answered_ping_is_up_whatever_launchd_says() -> None:
    result = hc.verdict(failure=None, job_text=_STUCK_JOB)
    assert result.alive
    assert result.detail == "ping answered; helper serving"


def test_probe_is_up_when_the_native_parent_of_the_root_answers(tmp_path: Path) -> None:
    launchctl = _Launchctl()
    with socket_dir() as directory, live_root() as root:
        sock_path = directory / "helper.sock"
        with serving(sock_path, wire_reply(os.getpid())):
            result = hc.probe(
                home=tmp_path, facts=_DARWIN, run=launchctl, sock_path=sock_path, root=root
            )
    assert result.alive
    assert launchctl.commands == []


def test_probe_classifies_a_helper_that_does_not_answer_from_its_own_launchd_job(
    tmp_path: Path,
) -> None:
    launchctl = _Launchctl(_STUCK_JOB)
    with socket_dir() as directory, live_root() as root:
        result = hc.probe(
            home=tmp_path,
            facts=_DARWIN,
            run=launchctl,
            sock_path=directory / "nobody-listens.sock",
            root=root,
        )
    assert not result.alive
    assert result.detail.startswith(hc.LWCR_STUCK)
    assert "PermissionsHelperError" in result.detail
    assert launchctl.commands == [
        ["launchctl", "print", f"{_DARWIN.domain}/{helper_job_label(tmp_path)}"]
    ]


def test_probe_reads_a_pong_that_is_not_true_as_a_failure_of_the_helper(tmp_path: Path) -> None:
    with socket_dir() as directory, live_root() as root:
        sock_path = directory / "helper.sock"
        with serving(sock_path, wire_reply(os.getpid(), pong=False)):
            result = hc.probe(
                home=tmp_path, facts=_DARWIN, run=_Launchctl(None), sock_path=sock_path, root=root
            )
    assert not result.alive
    assert result.detail.startswith(hc.ABSENT)
    assert "pong was not true" in result.detail


def test_probe_never_reports_health_for_a_peer_it_cannot_bind_to_the_root(tmp_path: Path) -> None:
    launchctl = _Launchctl(_RUNNING_JOB)
    with socket_dir() as directory, under_other_parent() as root:
        sock_path = directory / "helper.sock"
        with serving(sock_path, wire_reply(os.getpid())):
            result = hc.probe(
                home=tmp_path, facts=_DARWIN, run=launchctl, sock_path=sock_path, root=root
            )
    assert result.verdict.value == "unavailable"
    assert "ParentEvidenceError" in result.detail
    assert launchctl.commands == []


def test_probe_is_unavailable_when_the_job_cannot_be_read(tmp_path: Path) -> None:
    with socket_dir() as directory, live_root() as root:
        result = hc.probe(
            home=tmp_path,
            facts=_DARWIN,
            run=_Launchctl(error=RuntimeError("boom")),
            sock_path=directory / "nobody-listens.sock",
            root=root,
        )
    assert result.verdict.value == "unavailable"
    assert "probe error: RuntimeError: boom" in result.detail


def test_probe_off_macos_is_up_without_asking_anyone(tmp_path: Path) -> None:
    launchctl = _Launchctl()
    root = OwnedProcess(pid=os.getpid(), birth=1.0, starttime=None)
    result = hc.probe(
        home=tmp_path,
        facts=LaunchdFacts(uid=501, platform="linux", user_home=Path("/unused/user")),
        run=launchctl,
        sock_path=tmp_path / "unused.sock",
        root=root,
    )
    assert result.alive
    assert launchctl.commands == []


def test_episode_reporting_memory_is_local_to_each_diagnostic(recorder: _Recorder) -> None:
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
