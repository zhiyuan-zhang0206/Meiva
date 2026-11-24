# meiva:note
# The retained observer submits through the existing agent queue and never mints a receipt on failure.
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from cli.fleet_update import fleet_alert_silence
from scripts.host_ops.watchdog_sentinel import (
    DEFAULT_QUERY,
    DEFAULT_SILENCE_SECONDS,
    WINDOW_MARKER,
    Endpoints,
    Sentinel,
    age_from_payload,
)


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
    assert "no heartbeat sample in the last hour" in message
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


def _window(home: Path, until: datetime) -> None:
    fleet_alert_silence.write_window_end(home, until)


def test_the_sentinel_and_the_fleet_update_name_the_same_window_marker() -> None:
    assert WINDOW_MARKER == fleet_alert_silence.WINDOW_MARKER


def test_a_planned_update_window_holds_every_judgement_until_it_ends(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = None
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    sentinel.write_state({"first_seen": 500})
    _window(tmp_path, datetime.fromtimestamp(2000, UTC))

    assert sentinel.tick(now=1000) == 0
    assert sentinel.tick(now=1999) == 0
    assert recorder.sent == []
    assert "held planned update window" in sentinel.log_path.read_text()
    assert sentinel.read_state() == {"first_seen": 500}

    assert sentinel.tick(now=2000) == 0
    [message] = recorder.sent
    assert "no heartbeat sample in the last hour" in message


def test_a_window_holds_the_query_failure_leg_too(tmp_path: Path) -> None:
    recorder = Recorder()

    def read_age() -> float | None:
        raise ConnectionRefusedError("Prometheus stopped by the update")

    sentinel = Sentinel(home=tmp_path, endpoints=Endpoints(read_age, recorder.send))
    _window(tmp_path, datetime.fromtimestamp(2000, UTC))
    assert sentinel.tick(now=1000) == 0
    assert recorder.sent == []
    assert sentinel.tick(now=2001) == 1
    assert "could not query Prometheus" in recorder.sent[0]


def test_a_settled_window_ends_the_hold_at_the_settle_grace(tmp_path: Path) -> None:
    opened = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)
    _window(tmp_path, opened + timedelta(hours=4))
    fleet_alert_silence.settle_window_end(tmp_path, now=opened + timedelta(minutes=40))
    recorder = Recorder()
    recorder.age = 99999
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints(), silence_seconds=60)
    sentinel.write_state({"first_seen": 1})
    settled = (opened + timedelta(minutes=40) + fleet_alert_silence.SETTLE_WINDOW).timestamp()
    assert sentinel.tick(now=settled - 1) == 0
    assert recorder.sent == []
    assert sentinel.tick(now=settled) == 0
    assert len(recorder.sent) == 1


@pytest.mark.parametrize("content", [None, "", "not a timestamp\n"])
def test_no_or_unreadable_window_marker_holds_nothing(tmp_path: Path, content: str | None) -> None:
    if content is not None:
        (tmp_path / WINDOW_MARKER).parent.mkdir(parents=True)
        (tmp_path / WINDOW_MARKER).write_text(content)
    recorder = Recorder()
    recorder.age = 900
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints(), silence_seconds=60)
    assert sentinel.tick(now=1000) == 0
    assert len(recorder.sent) == 1


def test_the_query_ages_the_newest_heartbeat_across_a_restart_and_outlasts_the_budget() -> None:
    match = re.fullmatch(
        r"time\(\) - max\(max_over_time\(meiva_alerts_last_ingest_timestamp_seconds\[(\d+)h\]\)\)",
        DEFAULT_QUERY,
    )
    assert match is not None
    assert int(match.group(1)) * 3600 > DEFAULT_SILENCE_SECONDS


def test_an_age_across_a_restart_gap_within_the_budget_is_not_silent(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 945
    sentinel = Sentinel(home=tmp_path, endpoints=recorder.endpoints())
    sentinel.write_state({"first_seen": 1})
    assert sentinel.tick(now=1000) == 0
    assert recorder.sent == []
    recorder.age = DEFAULT_SILENCE_SECONDS + 1
    assert sentinel.tick(now=2000) == 0
    assert len(recorder.sent) == 1
