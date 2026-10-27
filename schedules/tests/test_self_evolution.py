# meiva:note
# The daily scan and the weekly trigger of the self-evolution schedules.
#
# What each outcome logs, wakes and reports is decided from values. The daily shell runs a real
# child process (`children/fake_scan.py`) and talks to a gateway on a loopback port. The weekly
# shell counts events over a real HTTP listener and runs its iteration against the throwaway
# Postgres.

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from base.agents.context import MeivaContext
from base.db import Database
from base.host.net.tests.local_http import serve_json
from meiva.gateway_client import use_client
from schedules.jobs.loop import CronSchedule, Trigger
from schedules.jobs.self_evolution_scan import DailyScan, run_daily_scan
from schedules.jobs.self_evolution_weekly import (
    FOLLOW_UP_TRIGGER,
    MAIN_TRIGGER,
    EventsApi,
    WeeklyState,
    count_events,
    handle_weekly_trigger,
    read_events_api,
    weekly_once,
)
from schedules.plans.loop_plan import LoopTiming
from schedules.plans.scan_outcome import (
    ScanMissing,
    ScanPlan,
    ScanRan,
    ScanTimedOut,
    parse_daily_report_agent,
    plan_scan,
)
from schedules.plans.weekly_trigger import WeeklyDecision, decide_follow_up, decide_main
from schedules.tests.support.gateway_directory import (
    AgentRowSpec,
    DirectoryPage,
    GatewayRequest,
    serve_gateway,
)
from schedules.tests.support.schedule_support import claimed_slots, insert_schedule

_NOW = datetime(2026, 9, 8, 0, 0, 20, tzinfo=UTC)
_STAMP = "2026-09-08T00:00:20+00:00"
_FAKE_SCAN = Path(__file__).parent / "children" / "fake_scan.py"


def test_a_clean_scan_wakes_nobody_and_sends_the_daily_report() -> None:
    plan = plan_scan(ScanRan(0, "all clear", ""), _NOW)

    assert plan == ScanPlan(
        (f"[{_STAMP}] scan rc=0", "all clear"), None, "[self-evolution daily 09-08]\nall clear"
    )


def test_exit_status_two_is_the_alert_and_carries_both_tails() -> None:
    plan = plan_scan(ScanRan(2, "bad runs", "warn"), _NOW)

    assert plan.wake_prompt == (
        f"Daily scan ALERT ({_STAMP}):\nbad runs\nwarn\nReview the daily report and act."
    )
    assert plan.log_lines == (f"[{_STAMP}] scan rc=2", "bad runs")
    assert plan.daily_report == "[self-evolution daily 09-08]\nbad runs"


def test_any_other_failing_status_wakes_the_agent_with_the_error_and_still_reports() -> None:
    plan = plan_scan(ScanRan(3, "partial", "boom"), _NOW)

    assert plan.log_lines == (f"[{_STAMP}] scan rc=3", "partial", "scan failed rc=3: boom")
    assert plan.wake_prompt == (
        "Daily scan failed rc=3:\nboom\nCheck the schedule log and daily_scan.py."
    )
    assert plan.daily_report == "[self-evolution daily 09-08]\npartial"


def test_only_the_tails_of_a_long_scan_are_reported() -> None:
    plan = plan_scan(ScanRan(2, "o" * 5000 + "end", "e" * 900 + "tail"), _NOW)

    assert plan.daily_report is not None
    assert plan.daily_report.endswith("o" * 1997 + "end")
    assert plan.wake_prompt is not None
    assert "e" * 496 + "tail" in plan.wake_prompt
    assert "e" * 497 + "tail" not in plan.wake_prompt


def test_a_timed_out_scan_wakes_the_agent_and_sends_no_report() -> None:
    assert plan_scan(ScanTimedOut(10800), _NOW) == ScanPlan(
        ("scan timed out after 10800s",),
        "Daily scan timed out after 10800s — check whether daily_scan.py is stuck.",
        None,
    )


def test_a_missing_script_is_reported_as_missing_not_as_an_alert() -> None:
    plan = plan_scan(ScanMissing("/home/skills/daily_scan.py"), _NOW)

    assert plan.wake_prompt is not None
    assert plan.wake_prompt.startswith(
        "Daily scan cannot run: daily_scan.py missing at /home/skills/daily_scan.py"
    )
    assert "ALERT" not in plan.wake_prompt
    assert plan.daily_report is None


@pytest.mark.parametrize(("raw", "expected"), [(None, 228), ("", None), ("  ", None), ("17", 17)])
def test_the_daily_report_recipient(raw: str | None, expected: int | None) -> None:
    assert parse_daily_report_agent(raw) == expected


@pytest.mark.parametrize("raw", ["x", "1.5", "0", "-2"])
def test_a_malformed_daily_report_recipient_stops_the_schedule(raw: str) -> None:
    with pytest.raises(RuntimeError, match="MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT must be"):
        parse_daily_report_agent(raw)


def _scan(
    *args: str, timeout: int = 30, report_agent: int | None = 228, script: Path = _FAKE_SCAN
) -> DailyScan:
    return DailyScan((sys.executable, str(_FAKE_SCAN), *args), script, timeout, report_agent)


_SELF_EVOLUTION = (
    DirectoryPage(
        "self-evolution", None, (AgentRowSpec(90, "self-evolution", "terminated"),), None
    ),
)


def _requests(requests: list[GatewayRequest], suffix: str) -> list[GatewayRequest]:
    return [request for request in requests if request.path.endswith(suffix)]


def test_an_alerting_scan_wakes_the_agent_and_sends_the_daily_report(
    sdk_environment: MeivaContext, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = _scan("--exit", "2", "--stdout", "bad runs", "--stderr", "warn")

    with serve_gateway(_SELF_EVOLUTION) as gateway, gateway.client() as client, use_client(client):
        run_daily_scan(_NOW, None, scan=scan)

    [wake] = _requests(gateway.requests, "/resurrect")
    assert wake.path == "/api/agents/90/resurrect"
    assert str(wake.body["prompt"]).startswith("Daily scan ALERT (")
    assert "bad runs\nwarn" in str(wake.body["prompt"])
    [report] = _requests(gateway.requests, "/messages")
    assert report.path == "/api/agents/228/messages"
    assert str(report.body["content"]).endswith("\nbad runs")
    assert "scan rc=2" in capsys.readouterr().out


def test_a_clean_scan_reaches_only_the_report_recipient(sdk_environment: MeivaContext) -> None:
    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        run_daily_scan(_NOW, None, scan=_scan("--stdout", "all clear"))

    [report] = gateway.requests
    assert report.path == "/api/agents/228/messages"
    assert str(report.body["content"]).endswith("\nall clear")


def test_an_explicitly_empty_report_recipient_skips_the_daily_report(
    sdk_environment: MeivaContext,
) -> None:
    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        run_daily_scan(_NOW, None, scan=_scan("--stdout", "all clear", report_agent=None))

    assert gateway.requests == []


def test_a_scan_that_outlives_its_bound_is_cut_off_and_wakes_the_agent(
    sdk_environment: MeivaContext,
) -> None:
    with serve_gateway(_SELF_EVOLUTION) as gateway, gateway.client() as client, use_client(client):
        run_daily_scan(_NOW, None, scan=_scan("--sleep", "30", timeout=1))

    [wake] = _requests(gateway.requests, "/resurrect")
    assert wake.body["prompt"] == (
        "Daily scan timed out after 1s — check whether daily_scan.py is stuck."
    )
    assert _requests(gateway.requests, "/messages") == []


def test_a_missing_scan_script_wakes_the_agent_without_running_anything(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    scan = _scan("--exit", "2", script=tmp_path / "absent" / "daily_scan.py")

    with serve_gateway(_SELF_EVOLUTION) as gateway, gateway.client() as client, use_client(client):
        run_daily_scan(_NOW, None, scan=scan)

    [wake] = _requests(gateway.requests, "/resurrect")
    assert str(wake.body["prompt"]).startswith("Daily scan cannot run: daily_scan.py missing at")
    assert _requests(gateway.requests, "/messages") == []


_PROMPT = "Run the skill for this week."


def test_a_quiet_week_skips_and_does_not_arm_the_follow_up() -> None:
    decision = decide_main(29999, _NOW, _PROMPT)

    assert decision == WeeklyDecision(
        (
            f"[{_STAMP}] self-evolution check: 29999 events in past 7 days (min=30000, high=200000)",
            f"[{_STAMP}] self-evolution skipped: 29999 < 30000 minimum",
        ),
        None,
        False,
    )


def test_an_ordinary_week_fires_without_arming_the_follow_up() -> None:
    decision = decide_main(30000, _NOW, _PROMPT)

    assert decision.prompt == _PROMPT
    assert decision.follow_up_next is False
    assert len(decision.log_lines) == 1


def test_a_busy_week_fires_and_arms_the_follow_up() -> None:
    decision = decide_main(200000, _NOW, _PROMPT)

    assert decision.prompt == _PROMPT
    assert decision.follow_up_next is True
    assert decision.log_lines[1].endswith("mid-week follow-up enabled (200000 >= 200000)")


def test_the_follow_up_bar_is_half_the_high_threshold_and_disarms() -> None:
    below = decide_follow_up(99999, _NOW, _PROMPT)
    at = decide_follow_up(100000, _NOW, _PROMPT)

    assert below.prompt is None
    assert at.prompt == f"{_PROMPT} (mid-week follow-up)"
    assert not below.follow_up_next and not at.follow_up_next
    assert "bar=100000" in at.log_lines[0]


def test_events_are_counted_with_the_total_flag_and_the_bearer(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=7)
    with serve_json({"meta": {"total": 42}}) as served:
        total = count_events(EventsApi(served.url, "s3cret"), since, _NOW)

    assert total == 42
    [request] = served.requests
    assert request.path.startswith("/api/events?")
    for part in ("with_total=1", "limit=1", "offset=0"):
        assert part in request.path
    assert request.headers["authorization"] == "Bearer s3cret"


def test_a_reply_without_a_total_fails_instead_of_counting_zero() -> None:
    with (
        serve_json({"meta": {}, "items": []}) as served,
        pytest.raises(RuntimeError, match="no total"),
    ):
        count_events(EventsApi(served.url, ""), _NOW - timedelta(days=1), _NOW)

    assert "authorization" not in served.requests[0].headers


def test_the_events_api_comes_from_the_environment_then_the_homes_env_file(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("OTHER=1\nMEIVA_CLUSTER_SECRET='file-secret'\n")
    delivered = read_events_api(
        {"MEIVA_API_TOKEN": "delivered", "MEIVA_GATEWAY_URL": "http://gw:1"}, tmp_path
    )
    from_file = read_events_api({}, tmp_path)

    assert delivered == EventsApi("http://gw:1", "delivered")
    assert from_file == EventsApi("http://localhost:8000", "file-secret")
    assert read_events_api({}, tmp_path / "empty-home") == EventsApi("http://localhost:8000", "")


def _weekly_api(url: str) -> EventsApi:
    return EventsApi(url, "")


def test_a_busy_main_check_wakes_the_agent_and_arms_the_follow_up(
    sdk_environment: MeivaContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        serve_json({"meta": {"total": 250000}}) as events,
        serve_gateway(_WEEKLY_AGENT) as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        armed = handle_weekly_trigger(
            _NOW, MAIN_TRIGGER, api=_weekly_api(events.url), prompt=_PROMPT
        )

    assert armed is True
    [wake] = _requests(gateway.requests, "/messages")
    assert wake.path == "/api/agents/90/messages"
    assert wake.body["content"] == _PROMPT
    assert "mid-week follow-up enabled (250000 >= 200000)" in capsys.readouterr().out


def test_a_quiet_main_check_wakes_nobody(sdk_environment: MeivaContext) -> None:
    with (
        serve_json({"meta": {"total": 10}}) as events,
        serve_gateway() as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        armed = handle_weekly_trigger(
            _NOW, MAIN_TRIGGER, api=_weekly_api(events.url), prompt=_PROMPT
        )

    assert armed is False
    assert gateway.requests == []


def test_the_follow_up_check_counts_four_days_and_disarms(sdk_environment: MeivaContext) -> None:
    with (
        serve_json({"meta": {"total": 150000}}) as events,
        serve_gateway(_WEEKLY_AGENT) as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        armed = handle_weekly_trigger(
            _NOW, FOLLOW_UP_TRIGGER, api=_weekly_api(events.url), prompt=_PROMPT
        )

    assert armed is False
    [wake] = _requests(gateway.requests, "/messages")
    assert wake.body["content"] == f"{_PROMPT} (mid-week follow-up)"


def test_an_unknown_weekly_trigger_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown self-evolution weekly trigger: monday"):
        handle_weekly_trigger(_NOW, "monday", api=EventsApi("http://unused", ""), prompt=_PROMPT)


_WEEKLY_AGENT = (
    DirectoryPage("self-evolution", None, (AgentRowSpec(90, "self-evolution", "idling"),), None),
)
_TUESDAY = datetime(2026, 9, 8, tzinfo=UTC)
_FRIDAY = datetime(2026, 9, 11, tzinfo=UTC)
_WEEKLY = CronSchedule(
    (Trigger("0 0 * * 2", MAIN_TRIGGER), Trigger("0 0 * * 5", FOLLOW_UP_TRIGGER)),
    "UTC",
    LoopTiming(fire_ahead=90.0),
)


def _arms_on_main(_slot: datetime, trigger: str | None) -> bool:
    return trigger == MAIN_TRIGGER


def test_the_main_fire_arms_the_follow_up_slot_and_the_follow_up_fire_disarms_it(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = insert_schedule(
        db_conn, name="weekly-state", created_at=_TUESDAY - timedelta(days=9)
    )

    def step(state: WeeklyState, now: datetime) -> tuple[WeeklyState, float]:
        return weekly_once(
            database, _WEEKLY, _arms_on_main, state=state, now=now, schedule_id=schedule_id
        )

    state = WeeklyState(_TUESDAY - timedelta(days=1), False)
    state, sleep = step(state, _TUESDAY + timedelta(seconds=10))
    assert (state, sleep) == (WeeklyState(_TUESDAY, True), 120.0)

    state, sleep = step(state, _TUESDAY + timedelta(seconds=130))
    assert (state, sleep) == (WeeklyState(_TUESDAY, True), 3600.0)

    state, sleep = step(state, _FRIDAY - timedelta(seconds=30))
    assert (state, sleep) == (WeeklyState(_FRIDAY, False), 120.0)

    assert claimed_slots(db_conn, schedule_id) == [_TUESDAY, _FRIDAY]


def test_an_unarmed_loop_does_not_look_for_the_follow_up_slot(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = insert_schedule(
        db_conn, name="weekly-unarmed", created_at=_TUESDAY - timedelta(days=9)
    )
    state = WeeklyState(_TUESDAY + timedelta(minutes=5), False)

    state, sleep = weekly_once(
        database,
        _WEEKLY,
        _arms_on_main,
        state=state,
        now=_FRIDAY + timedelta(seconds=10),
        schedule_id=schedule_id,
    )

    assert (state.follow_up_armed, sleep) == (False, 3600.0)
    assert claimed_slots(db_conn, schedule_id) == []
