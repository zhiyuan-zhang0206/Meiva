# meiva:note
# The database write-stall sentinel.
#
# When a minute counts as stalled and when the operators are told is decided from PgBouncer's
# stats lines (`plan_poll`). The shell reads a real log file, keeps a real state file, and sends
# alerts through a gateway on a loopback port; `run_watch` runs in a child process.

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.agents.context import MeivaContext
from meiva.gateway_client import use_client
from schedules.jobs.stall_watch import StallWatch, initial_state, poll_once
from schedules.plans.stall_rule import StallSample, WatchState, parse_stats, plan_poll
from schedules.tests.support.gateway_directory import serve_gateway

_CHILD = Path(__file__).parent / "children" / "stall_watch_child.py"


def _line(minute: int, xacts: int, query_us: int) -> str:
    return (
        f"2026-10-24 11:{minute:02d}:03.456 CST [4242] LOG stats: {xacts} xacts/s, "
        f"8 queries/s, 0 client parses/s, 0 server parses/s, 0 binds/s, in 120 B/s, "
        f"out 480 B/s, xact 900 us, query {query_us} us, wait 12 us"
    )


def _sample(minute: int, xacts: int, query_us: int) -> StallSample:
    return StallSample(f"2026-10-24 11:{minute:02d}:03.456", xacts, 8, query_us)


_STALLED = (2, 400_000)
_HEALTHY = (200, 900)
_EMPTY = WatchState(None, (), 0.0, False)


def test_only_stats_lines_are_samples() -> None:
    lines = ["2026-10-24 11:00:00.000 CST [1] LOG C-0x1: new connection", _line(1, 5, 500_000)]

    assert parse_stats(lines) == (_sample(1, 5, 500_000),)


def test_two_stalled_minutes_are_logged_but_do_not_page_anyone() -> None:
    plan = plan_poll(_EMPTY, [_sample(1, *_STALLED), _sample(2, *_STALLED)], now=1000.0)

    assert plan.alert is None
    assert [line.split(":")[0] for line in plan.log_lines] == ["stalled", "stalled"]
    assert plan.advanced
    assert plan.state.seen == "2026-10-24 11:02:03.456"


def test_three_consecutive_stalled_minutes_page_once_with_the_evidence() -> None:
    samples = [_sample(minute, *_STALLED) for minute in (1, 2, 3)]

    plan = plan_poll(_EMPTY, samples, now=5000.0)

    assert plan.alert is not None
    assert plan.alert.startswith(
        "DB write path stalled for >= 3 consecutive minutes "
        "(xacts/s < 20 and average query > 300 ms):\n"
    )
    assert "11:03:03.456 2 xacts/s query=400ms" in plan.alert
    assert plan.state.last_alert == 5000.0 and plan.state.alerted


def test_a_slow_minute_with_throughput_or_a_fast_one_without_is_not_stalled() -> None:
    samples = [_sample(1, 100, 400_000), _sample(2, 2, 100_000), _sample(3, 2, 300_000)]

    plan = plan_poll(_EMPTY, samples, now=5000.0)

    assert plan.alert is None
    assert plan.log_lines == ()


def test_the_cooldown_keeps_one_incident_from_paging_every_minute() -> None:
    first = plan_poll(_EMPTY, [_sample(m, *_STALLED) for m in (1, 2, 3)], now=10_000.0)
    inside = plan_poll(first.state, [_sample(4, *_STALLED)], now=10_000.0 + 1799.0)
    after = plan_poll(inside.state, [_sample(5, *_STALLED)], now=10_000.0 + 1800.0)

    assert first.alert is not None
    assert inside.alert is None
    assert after.alert is not None


def test_a_healthy_minute_after_an_alert_closes_the_incident() -> None:
    paged = plan_poll(_EMPTY, [_sample(m, *_STALLED) for m in (1, 2, 3)], now=10_000.0)

    healed = plan_poll(paged.state, [_sample(4, *_HEALTHY)], now=10_100.0)

    assert healed.log_lines == ("recovered: 2026-10-24 11:04:03.456 200 xacts/s query=1ms",)
    assert not healed.state.alerted


def test_samples_already_seen_are_skipped() -> None:
    seen = plan_poll(_EMPTY, [_sample(1, *_STALLED), _sample(2, *_STALLED)], now=1.0)

    again = plan_poll(seen.state, [_sample(1, *_STALLED), _sample(2, *_STALLED)], now=2.0)

    assert not again.advanced
    assert again.log_lines == ()
    assert again.state == seen.state


def test_the_window_remembers_twelve_minutes() -> None:
    samples = [_sample(minute, *_HEALTHY) for minute in range(20)]

    plan = plan_poll(_EMPTY, samples, now=1.0)

    assert [sample.stamp[14:16] for sample in plan.state.window] == [
        f"{m:02d}" for m in range(8, 20)
    ]


def _watch(tmp_path: Path, agents: tuple[int, ...] = (1818, 405)) -> StallWatch:
    return StallWatch(
        log=tmp_path / "pgbouncer.log",
        state=tmp_path / "run" / "db-write-stall-watch.json",
        history=tmp_path / "state" / "db-write-stall-history.jsonl",
        zone=ZoneInfo("Asia/Shanghai"),
        report_agents=agents,
    )


def test_three_stalled_minutes_in_the_log_page_every_operator_and_save_the_state(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch = _watch(tmp_path)
    watch.log.write_text("\n".join(_line(m, *_STALLED) for m in (1, 2, 3)) + "\n")

    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        state = poll_once(watch, initial_state(watch), now=4000.0)

    assert sorted(request.path for request in gateway.requests) == [
        "/api/agents/1818/messages",
        "/api/agents/405/messages",
    ]
    assert str(gateway.requests[0].body["content"]).startswith("DB write path stalled")
    saved = json.loads(watch.state.read_text())
    assert saved["last_alert"] == 4000.0
    assert saved["last_sample"] == "2026-10-24 11:03:03.456"
    assert state.alerted
    assert "] stalled: 2026-10-24 11:01:03.456 2 xacts/s query=400ms" in capsys.readouterr().out


def test_an_operator_that_cannot_be_reached_does_not_stop_the_others(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch = _watch(tmp_path)
    watch.log.write_text("\n".join(_line(m, *_STALLED) for m in (1, 2, 3)) + "\n")

    with (
        serve_gateway(refuse=frozenset({"/api/agents/1818/messages"})) as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        poll_once(watch, initial_state(watch), now=4000.0)

    assert "db-write-stall-watch could not notify 1818" in capsys.readouterr().out
    assert [request.path for request in gateway.requests] == [
        "/api/agents/1818/messages",
        "/api/agents/405/messages",
    ]


def test_a_restart_inside_the_cooldown_does_not_page_again(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    watch = _watch(tmp_path)
    watch.log.write_text("\n".join(_line(m, *_STALLED) for m in (1, 2, 3)) + "\n")
    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        poll_once(watch, initial_state(watch), now=time.time())
        saved_alert = json.loads(watch.state.read_text())["last_alert"]
        restarted = initial_state(watch)
        again = poll_once(watch, restarted, now=time.time() + 10)

    assert len(gateway.requests) == 2
    assert restarted.last_alert == saved_alert
    assert again.last_alert == saved_alert


def test_a_missing_log_is_an_empty_poll(tmp_path: Path) -> None:
    watch = _watch(tmp_path, agents=())

    state = poll_once(watch, initial_state(watch), now=1.0)

    assert state == initial_state(watch)
    assert not watch.state.exists()


def test_the_watch_loop_reads_the_log_and_keeps_its_state_file(tmp_path: Path) -> None:
    log = tmp_path / "pgbouncer.log"
    state = tmp_path / "state.json"
    log.write_text("\n".join(_line(m, *_HEALTHY) for m in (1, 2)) + "\n")
    child = subprocess.Popen([sys.executable, "-I", str(_CHILD), str(log), str(state)])
    try:
        deadline = time.monotonic() + 30
        while not state.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert json.loads(state.read_text())["last_sample"] == "2026-10-24 11:02:03.456"


def test_every_new_minute_lands_in_the_history_file(tmp_path: Path) -> None:
    watch = _watch(tmp_path, agents=())
    watch.log.write_text("\n".join(_line(m, *_STALLED) for m in (1, 2, 3)) + "\n")

    poll_once(watch, initial_state(watch), now=4000.0)

    rows = [json.loads(line) for line in watch.history.read_text().splitlines()]
    assert [row["stamp"] for row in rows] == [
        "2026-10-24 11:01:03.456",
        "2026-10-24 11:02:03.456",
        "2026-10-24 11:03:03.456",
    ]
    assert rows[0] == {
        "stamp": "2026-10-24 11:01:03.456",
        "xacts_per_s": 2,
        "queries_per_s": 8,
        "query_us": 400_000,
        "alert_sent": True,
        "source": "pgbouncer stats",
    }


def test_a_repeat_poll_does_not_duplicate_history(tmp_path: Path) -> None:
    watch = _watch(tmp_path, agents=())
    watch.log.write_text("\n".join(_line(m, *_HEALTHY) for m in (1, 2)) + "\n")

    state = poll_once(watch, initial_state(watch), now=1.0)
    poll_once(watch, state, now=2.0)

    assert len(watch.history.read_text().splitlines()) == 2


def test_the_history_keeps_only_its_newest_lines(tmp_path: Path) -> None:
    watch = _watch(tmp_path, agents=())
    watch.history.parent.mkdir(parents=True, exist_ok=True)
    watch.history.write_text(
        "\n".join(json.dumps({"stamp": f"old-{index}"}) for index in range(20_000)) + "\n"
    )
    watch.log.write_text(_line(1, *_HEALTHY) + "\n")

    poll_once(watch, initial_state(watch), now=1.0)

    rows = watch.history.read_text().splitlines()
    assert len(rows) == 15_000
    assert json.loads(rows[-1])["stamp"] == "2026-10-24 11:01:03.456"
