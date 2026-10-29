# meiva:note
# The database beat's failure and recovery are structured events (task #5767), so "what did the
# beat see before the host exited" is answerable from the event stream. A log line that merely
# mentions the words is not the event — the control below keeps the two apart.
# meiva:link agent/docs/loop.meiva.okf.md

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import cast

import psycopg
import pytest
from loguru import logger

from base.agents.observation.turn_progress import TurnProgress
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_async_redis

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


def _host(reaper) -> object:
    class FakeHost:
        admission = _FakeAdmission()
        turn_progress = TurnProgress()

        async def reap_corpses(self) -> None:
            await reaper()

    return FakeHost()


def _events(records: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [record for record in records if record.get("event") == name]


async def _run_beat(host, *, steps: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(dict(message.record["extra"])), level="DEBUG")
    task = asyncio.create_task(
        host_beat.beat_forever(
            cast(host_daemon.Liveness, _FakeLiveness()),
            cast(host_daemon.AgentHost, host),
            cast(host_daemon.TurnScheduler, _FakeScheduler()),
            "runner-a",
            EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            require_helper_chain=lambda: None,
            read_alert_seconds=lambda: 30.0,
            publish_timeout_s=0.001,
            beat_step_s=0.001,
        )
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(_events(records, "host_database_beat_failed")) >= steps:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, psycopg.OperationalError):
            await task
        logger.remove(sink)
    return records


async def test_each_failed_beat_is_one_event_and_the_third_one_says_it_ends_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def always_fails() -> None:
        raise psycopg.OperationalError("connection refused")

    patch_async_redis(monkeypatch, _OkRedis)
    records = await _run_beat(_host(always_fails), steps=3)

    failures = _events(records, "host_database_beat_failed")
    assert [record["failures"] for record in failures[:3]] == [1, 2, 3]
    assert [record["will_exit"] for record in failures[:3]] == [False, False, True]
    assert failures[0]["machine"] == "runner-a"
    assert failures[0]["pool"] == "host-reaper"
    assert failures[0]["error"] == "OperationalError"
    assert failures[0]["limit"] == 3


async def test_a_beat_that_recovers_reports_what_it_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fails_twice_then_works() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 2:
            raise psycopg.OperationalError("connection refused")

    patch_async_redis(monkeypatch, _OkRedis)
    records = await _run_beat(_host(fails_twice_then_works), steps=2)
    for _ in range(200):
        await asyncio.sleep(0.005)
        if _events(records, "host_database_beat_recovered"):
            break

    recovered = _events(records, "host_database_beat_recovered")
    assert recovered and recovered[0]["failures_cleared"] == 2
    assert recovered[0]["machine"] == "runner-a"
    assert [record["failures"] for record in _events(records, "host_database_beat_failed")] == [
        1,
        2,
    ]


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
