# meiva:note
# Process-level checks for the alert-pipeline sentinel: the credential spelling the home actually
# uses, the unarmed start, one message per incident, the escalation latch on both delivery legs,
# the recovery report, and the escalation argv.
# meiva:link scripts/host_ops/docs/watchdog-sentinel.meiva.okf.md
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.host_ops.watchdog_sentinel import (
    Endpoints,
    Sentinel,
    TelegramSendError,
    age_from_payload,
    parse_env,
    pick,
)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.escalated: list[str] = []
        self.age: float | None = None
        self.probe_error: TelegramSendError | None = None
        self.send_error: TelegramSendError | None = None

    def read_age(self) -> float | None:
        return self.age

    def send(self, text: str) -> dict[str, Any]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)
        return {"ok": True, "result": {"message_id": 7, "date": 1793795595}}

    def probe(self) -> None:
        if self.probe_error is not None:
            raise self.probe_error

    def escalate(self, message: str) -> bool:
        self.escalated.append(message)
        return True

    def endpoints(self) -> Endpoints:
        return Endpoints(
            age=self.read_age,
            send=self.send,
            probe=self.probe,
            escalate=self.escalate,
        )


def build(tmp_path: Path, recorder: Recorder, **fields: Any) -> Sentinel:
    return Sentinel(home=tmp_path, endpoints=recorder.endpoints(), **fields)


def test_pick_reads_the_spelling_the_home_defines() -> None:
    values = parse_env('MEIVA_TELEGRAM_OWNER_ID="42"\n# MEIVA_TELEGRAM_OWNER_ID=7\n')

    assert pick(values, ("MEIVA_ALERTS_TELEGRAM_CHAT_ID", "MEIVA_TELEGRAM_OWNER_ID")) == "42"
    assert pick({}, ("MEIVA_ALERTS_TELEGRAM_CHAT_ID",)) == ""


def test_age_from_payload_separates_empty_from_zero() -> None:
    assert age_from_payload({"data": {"result": [{"value": [1, "42.5"]}]}}) == 42.5
    assert age_from_payload({"data": {"result": []}}) is None
    assert age_from_payload({"data": {}}) is None
    assert age_from_payload({}) is None


def test_no_sample_yet_is_unarmed_rather_than_silent(tmp_path: Path) -> None:
    recorder = Recorder()
    sentinel = build(tmp_path, recorder)

    assert sentinel.tick(now=1000.0) == 0
    assert recorder.sent == []
    assert "unarmed" in sentinel.log_path.read_text(encoding="utf-8")
    assert not sentinel.state_path.exists()


def test_a_silent_pipeline_pages_once_per_repeat_window(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900.0
    sentinel = build(tmp_path, recorder, silence_seconds=60.0, repeat_hours=6.0)

    assert sentinel.tick(now=1000.0) == 0
    assert len(recorder.sent) == 1
    assert "silent for over 60s" in recorder.sent[0]

    assert sentinel.tick(now=2000.0) == 0
    assert len(recorder.sent) == 1

    assert sentinel.tick(now=1000.0 + 6 * 3600) == 0
    assert len(recorder.sent) == 2


def test_a_permanent_send_failure_escalates_at_once_and_only_once(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900.0
    recorder.send_error = TelegramSendError(401, "Unauthorized")
    sentinel = build(tmp_path, recorder, silence_seconds=60.0)

    assert sentinel.tick(now=1000.0) == 1
    assert len(recorder.escalated) == 1
    assert "401" in recorder.escalated[0]

    assert sentinel.tick(now=1010.0) == 1
    assert len(recorder.escalated) == 1


def test_three_transient_send_failures_escalate(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900.0
    recorder.send_error = TelegramSendError(0, "timed out")
    sentinel = build(tmp_path, recorder, silence_seconds=60.0)

    assert sentinel.tick(now=1000.0) == 1
    assert sentinel.tick(now=1001.0) == 1
    assert recorder.escalated == []

    assert sentinel.tick(now=1002.0) == 1
    assert len(recorder.escalated) == 1


def test_a_recovered_send_reports_the_outage_and_clears_the_latch(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 900.0
    recorder.send_error = TelegramSendError(401, "Unauthorized")
    sentinel = build(tmp_path, recorder, silence_seconds=60.0)

    assert sentinel.tick(now=1000.0) == 1
    assert recorder.escalated

    recorder.send_error = None
    assert sentinel.tick(now=1000.0 + 6 * 3600) == 0
    assert len(recorder.sent) == 2
    assert "silent for over 60s" in recorder.sent[0]
    assert "recovered" in recorder.sent[1]
    assert "RECOVERED telegram send" in sentinel.log_path.read_text(encoding="utf-8")


def test_a_blocked_bot_escalates_on_the_first_probe(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.age = 10.0
    recorder.probe_error = TelegramSendError(403, "Forbidden: bot was blocked by the user")
    sentinel = build(tmp_path, recorder)

    assert sentinel.tick(now=1000.0) == 0
    assert len(recorder.escalated) == 1
    assert "403" in recorder.escalated[0]
    assert "ERROR delivery probe failed" in sentinel.log_path.read_text(encoding="utf-8")

    recorder.probe_error = None
    assert sentinel.tick(now=1010.0) == 0
    assert len(recorder.sent) == 1
    assert "recovered" in recorder.sent[0]
    assert "ok age=" in sentinel.log_path.read_text(encoding="utf-8")


def test_missing_telegram_credentials_are_permanent(tmp_path: Path) -> None:
    sentinel = Sentinel(home=tmp_path)

    with pytest.raises(TelegramSendError) as raised:
        sentinel.telegram("hello")

    assert raised.value.status == 401


def test_escalation_runs_the_cli_and_reports_its_exit_code(tmp_path: Path) -> None:
    working = Sentinel(home=tmp_path, meiva_bin=Path("/bin/echo"))
    assert working.escalate("boom") is True
    assert "ESCALATED" in working.log_path.read_text(encoding="utf-8")

    failing = Sentinel(home=tmp_path / "second", meiva_bin=Path("/usr/bin/false"))
    assert failing.escalate("boom") is False
    assert "ERROR escalation rc=1" in failing.log_path.read_text(encoding="utf-8")
