# meiva:note
# The database beat's failure and recovery are structured events (task #5767), so "what did the
# beat see before the host exited" is answerable from the event stream. A log line that merely
# mentions the words is not the event — the control below keeps the two apart.
# meiva:link agent/docs/loop.meiva.okf.md

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import cast

import psycopg
import pytest
from loguru import logger

from base.agents.observation.turn_progress import TurnProgress
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.live.tests.fakes import async_redis_factory

from ... import daemon as host_daemon
from ... import host_beat


class _FakeAdmission:
    limit = 0
    waiting = 0

    def long_waiters(self, threshold: float) -> list[tuple[int, float]]:
        return []

    def payload(self) -> dict[str, object]:
        return {}


class _OkRedis:
    async def set(self, key: str, value: str, *, ex: int) -> None:
        del key, value, ex


class _FakeLiveness:
    def beat(self) -> None:
        pass


class _FakeScheduler:
    active_agents: frozenset[int] = frozenset()


def _refused() -> psycopg.OperationalError:
    return psycopg.OperationalError("connection refused")


# meiva:note
# A beat run has to end where the script says, not where a timer or a cancel lands: the recovery
# case below went red on 2026-10-29 (shard 8/8) because the test cancelled the loop as soon as it
# saw the second failure — before the beat that recovers — and then waited for a report no live
# task could still write. `beat_forever` is a `while True`, so the run is stopped from inside the
# reap it is awaiting: attempt N + 1 cannot begin before attempt N's reports are on the log
# stream, so every report a test asserts on has already happened when the run ends.
class _Reaps:
    def __init__(self, *outcomes: psycopg.OperationalError | None) -> None:
        self._outcomes = outcomes
        self.attempts = 0

    async def __call__(self) -> None:
        if self.attempts == len(self._outcomes):
            raise asyncio.CancelledError
        outcome = self._outcomes[self.attempts]
        self.attempts += 1
        if outcome is not None:
            raise outcome


def _host(reaper: Callable[[], Awaitable[None]]) -> object:
    class FakeHost:
        admission = _FakeAdmission()
        turn_progress = TurnProgress()

        async def reap_corpses(self) -> None:
            await reaper()

    return FakeHost()


def _events(records: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [record for record in records if record.get("event") == name]


# meiva:note
# The run ends from inside the reap, not here; the bound is a stall guard, so a change that stops
# the loop from reaching its reap fails instead of hanging the shard.
async def _run_beat(host: object) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(dict(message.record["extra"])), level="DEBUG")
    task = asyncio.create_task(
        host_beat.beat_forever(
            cast(host_daemon.Liveness, _FakeLiveness()),
            cast(host_daemon.AgentHost, host),
            cast(host_daemon.TurnScheduler, _FakeScheduler()),
            "runner-a",
            EventBus(
                event_bus_config_from_settings(),
                rng=seeded_rng(),
                timers=SYSTEM_TIMERS,
                open_async_redis=async_redis_factory(_OkRedis),
            ),
            require_helper_chain=lambda: None,
            read_alert_seconds=lambda: 30.0,
            publish_timeout_s=0.001,
            beat_step_s=0.001,
        )
    )
    try:
        with contextlib.suppress(asyncio.CancelledError, psycopg.OperationalError):
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        logger.remove(sink)
    return records


# meiva:note
# The third failure raises out of the loop, so no fourth beat runs: `attempts` stays at three and
# the `will_exit` flag is what ends the host rather than a cancel from the test.
async def test_each_failed_beat_is_one_event_and_the_third_one_says_it_ends_the_host() -> None:
    reaps = _Reaps(_refused(), _refused(), _refused())
    records = await _run_beat(_host(reaps))

    failures = _events(records, "host_database_beat_failed")
    assert [record["failures"] for record in failures] == [1, 2, 3]
    assert [record["will_exit"] for record in failures] == [False, False, True]
    assert failures[0]["machine"] == "runner-a"
    assert failures[0]["pool"] == "host-reaper"
    assert failures[0]["error"] == "OperationalError"
    assert failures[0]["limit"] == 3
    assert reaps.attempts == 3


# meiva:note
# The fourth beat is clean too, so a recovery report that repeated after every later clean beat
# would arrive as a second event instead of passing for one.
async def test_a_beat_that_recovers_reports_what_it_cleared() -> None:
    reaps = _Reaps(_refused(), _refused(), None, None)
    records = await _run_beat(_host(reaps))

    recovered = _events(records, "host_database_beat_recovered")
    assert [record["failures_cleared"] for record in recovered] == [2]
    assert recovered[0]["machine"] == "runner-a"
    assert [record["failures"] for record in _events(records, "host_database_beat_failed")] == [
        1,
        2,
    ]
    assert reaps.attempts == 4


def test_a_log_line_that_merely_says_the_words_is_not_the_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(dict(message.record["extra"])), level="DEBUG")
    try:
        with caplog.at_level(logging.WARNING, logger=host_beat.host_log.name):
            host_beat.host_log.warning(
                "[agent-host] database beat failed (1/3): database renewal failed"
            )
    finally:
        logger.remove(sink)

    assert _events(records, "host_database_beat_failed") == []
    assert any("database renewal failed" in record.message for record in caplog.records)
