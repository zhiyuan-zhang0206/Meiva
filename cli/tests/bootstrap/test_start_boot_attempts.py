from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from base.host.system import boot_policy
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli import start_intent
from cli.parsers import build_parser

_BOOT_TIME = 1_000_000.0


def _boot(home: Path, code: int, *, macos: bool) -> int:
    return start_intent.run_boot_attempt(home, lambda: code, boot_time=_BOOT_TIME, macos=macos)


def test_the_parser_marks_a_boot_start_only_when_asked() -> None:
    parser = build_parser()
    assert parser.parse_args(["start", "--all-services"]).boot is False
    assert parser.parse_args(["start", "--all-services", "--boot"]).boot is True


@pytest.mark.parametrize("macos", [True, False])
def test_failed_attempts_before_the_budget_keep_their_exit_code(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
    macos: bool,
) -> None:
    codes = [_boot(tmp_path, 75, macos=macos) for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1)]
    assert codes == [75] * (boot_policy.BOOT_MAX_ATTEMPTS - 1)
    assert len(emitted_events) == 0


def test_the_attempt_that_spends_the_budget_alerts_and_stops_launchd(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1):
        assert _boot(tmp_path, 75, macos=True) == 75
    assert _boot(tmp_path, 75, macos=True) == 0
    (event,) = emitted_events
    assert (event.category, event.event_name) == ("telemetry", "boot_retries_exhausted")
    assert event.level == "error"
    assert event.attributes == {"attempts": boot_policy.BOOT_MAX_ATTEMPTS, "exit_code": 75}
    assert "not retrying" in capsys.readouterr().err


def test_on_linux_the_spending_attempt_still_fails_for_systemd(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = [_boot(tmp_path, 75, macos=False) for _ in range(boot_policy.BOOT_MAX_ATTEMPTS)]
    assert codes[-1] == 75
    assert len(emitted_events) == 1


def test_a_start_after_the_budget_still_runs_and_success_clears_the_count(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        _boot(tmp_path, 75, macos=True)
    ran: list[int] = []

    def recovered() -> int:
        ran.append(1)
        return 0

    assert start_intent.run_boot_attempt(tmp_path, recovered, boot_time=_BOOT_TIME, macos=True) == 0
    assert ran == [1]
    assert boot_policy.begin_attempt(tmp_path, boot_time=_BOOT_TIME) == 1


def test_run_start_hands_the_boot_flag_to_the_start_and_wraps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def once(args: Any, runtime: Any, retained: Any, *, boot: bool = False, **_owners: Any) -> int:
        seen["boot"] = boot
        return 0

    def wrapped(home: Path, start: Any, **_clock: Any) -> int:
        seen["home"] = home
        return start()

    monkeypatch.setattr(start_intent, "start_once", once)
    monkeypatch.setattr(start_intent, "run_boot_attempt", wrapped)
    monkeypatch.setattr(start_intent, "resolve_home", lambda: tmp_path)

    def run(args: Any) -> int:
        return start_intent.run_start(
            args,
            retained_children=[],
            database_factory=object,
            producer=cast(Any, object),
        )

    args = build_parser().parse_args(["start", "--all-services", "--boot"])
    assert run(args) == 0
    assert seen == {"boot": True, "home": tmp_path}
    args = build_parser().parse_args(["start", "--all-services"])
    seen.clear()
    assert run(args) == 0
    assert seen == {"boot": False}
