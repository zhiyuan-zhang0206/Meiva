# meiva:note
# The retained observer submits through the existing agent queue and never mints a receipt on failure.
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

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


def _vector(*samples: object) -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": list(samples)}}


def test_age_from_payload_separates_empty_from_zero() -> None:
    assert age_from_payload(_vector({"value": [1, "0"]})) == 0
    assert age_from_payload(_vector()) is None


@pytest.mark.parametrize("payload", [{}, _vector({}), _vector({"value": [1, "NaN"]})])
def test_invalid_response_is_a_query_failure(payload: object) -> None:
    with pytest.raises(ValueError):
        age_from_payload(payload)


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
    assert "submitted to the agent message queue" in working.diagnostic_path.read_text()
    failing = Sentinel(home=tmp_path / "failed", meiva_bin=Path("/usr/bin/false"))
    assert failing.escalate("incident") is False
    assert "ERROR escalation rc=1" in failing.diagnostic_path.read_text()


def test_missing_sample_after_first_sighting_uses_the_silence_escalation(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 1
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    assert sentinel.tick(now=1000) == 0

    recorder.age = None
    assert sentinel.tick(now=1900) == 0
    assert sentinel.tick(now=2800) == 0

    [message] = recorder.sent
    assert "heartbeat series disappeared" in message
    assert "SILENT" in sentinel.log_path.read_text()
    assert sentinel.read_state() == {"first_seen": 1000, "last_sent": 1900}


def test_query_failure_notifies_without_spending_the_heartbeat_window(tmp_path: Path) -> None:
    recorder = Recorder()

    def read_age() -> float | None:
        if recorder.age is not None:
            return recorder.age
        return age_from_payload(_vector({}))

    sentinel = Sentinel(home=tmp_path, endpoints=Endpoints(read_age, recorder.send))
    sentinel.write_state({"first_seen": 1000})
    assert sentinel.tick(now=1900) == 1
    assert sentinel.tick(now=2800) == 1
    [message] = recorder.sent
    assert "could not query Prometheus" in message
    assert not sentinel.log_path.exists()
    assert sentinel.read_state() == {"first_seen": 1000, "query_failed_last_sent": 1900}

    recorder.age = 3600
    assert sentinel.tick(now=3000) == 0
    assert len(recorder.sent) == 2
    assert "pipeline is silent" in recorder.sent[-1]
    assert "SILENT" in sentinel.log_path.read_text()


def test_query_failure_retries_when_submission_fails(tmp_path: Path) -> None:
    recorder = Recorder()

    def read_age() -> float | None:
        raise ConnectionRefusedError("Prometheus stopped")

    sentinel = Sentinel(home=tmp_path, endpoints=Endpoints(read_age, recorder.send))
    recorder.accepted = False
    assert sentinel.tick(now=1000) == 1
    assert "query_failed_last_sent" not in sentinel.read_state()
    recorder.accepted = True
    assert sentinel.tick(now=1900) == 1
    assert sentinel.read_state() == {"query_failed_last_sent": 1900}


def test_overlapping_ticks_submit_once(tmp_path: Path) -> None:
    recorder = Recorder()
    sending = Event()
    finished = Event()

    def submit(message: str) -> bool:
        sending.set()
        assert finished.wait(timeout=5)
        return recorder.send(message)

    recorder.age = 3600
    first = Sentinel(home=tmp_path, endpoints=Endpoints(recorder.read_age, submit))
    second = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    with ThreadPoolExecutor() as pool:
        tick = pool.submit(first.tick, now=1000)
        try:
            assert sending.wait(timeout=5)
            assert second.tick(now=1001) == 0
        finally:
            finished.set()
        assert tick.result(timeout=5) == 0

    assert len(recorder.sent) == 1
    assert first.read_state()["last_sent"] == 1000
