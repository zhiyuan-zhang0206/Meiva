# meiva:note
# The retained observer submits through the existing agent queue and never mints a receipt on failure.
from pathlib import Path

from scripts.host_ops.watchdog_sentinel import Endpoints, Sentinel, age_from_payload


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.age: float | None = None
        self.accepted = True

    def read_age(self) -> float | None:
        return self.age

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.accepted

    def endpoints(self) -> Endpoints:
        return Endpoints(age=self.read_age, send=self.send)


def test_age_from_payload_separates_empty_from_zero() -> None:
    assert age_from_payload({"data": {"result": [{"value": [1, "42.5"]}]}}) == 42.5
    assert age_from_payload({"data": {"result": []}}) is None
    assert age_from_payload({}) is None


def test_no_sample_yet_is_unarmed_rather_than_silent(tmp_path: Path) -> None:
    recorder = Recorder()
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    assert sentinel.tick(now=1000) == 0
    assert recorder.sent == []
    assert not sentinel.state_path.exists()


def test_healthy_sample_does_not_submit_a_message(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 1
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    assert sentinel.tick(now=1000) == 0
    assert recorder.sent == []


def test_a_silent_pipeline_submits_once_per_repeat_window(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900
    sentinel = Sentinel(
        home=tmp_path, endpoints=recorder.endpoints(), silence_seconds=60, repeat_hours=6
    )
    assert sentinel.tick(now=1000) == 0
    assert sentinel.tick(now=2000) == 0
    assert len(recorder.sent) == 1
    assert sentinel.tick(now=1000 + 6 * 3600) == 0
    assert len(recorder.sent) == 2


def test_failed_submission_stays_retryable_and_returns_nonzero(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900
    recorder.accepted = False
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints(), silence_seconds=60)
    assert sentinel.tick(now=1000) == 1
    assert "last_sent" not in sentinel.read_state()
    recorder.accepted = True
    assert sentinel.tick(now=1001) == 0
    assert sentinel.read_state()["last_sent"] == 1001


def test_dry_run_never_submits_or_marks_delivery(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900
    sentinel = Sentinel(
        home=tmp_path, endpoints=recorder.endpoints(), silence_seconds=60, dry_run=True
    )
    assert sentinel.tick(now=1000) == 0
    assert recorder.sent == []
    assert "last_sent" not in sentinel.read_state()


def test_escalation_runs_the_installed_cli_and_reports_its_exit_code(tmp_path: Path) -> None:
    working = Sentinel(home=tmp_path, meiva_bin=Path("/bin/echo"))
    assert working.escalate("incident") is True
    assert "submitted to the agent message queue" in working.log_path.read_text()
    failing = Sentinel(home=tmp_path / "failed", meiva_bin=Path("/usr/bin/false"))
    assert failing.escalate("incident") is False
    assert "ERROR escalation rc=1" in failing.log_path.read_text()
