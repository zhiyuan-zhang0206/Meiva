# meiva:note
# Rate-limited collectors: the card keeps its last reading and age, and the cycle loop backs off.
# meiva:link meiva_builtins/plugins/meiva_usage/README.md

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg

from base.daemon.schedules.inputs import ScheduleInputs
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.probes import HttpJsonRequest, HttpStatusError
from meiva_builtins.plugins.meiva_usage.runner import (
    refresh_collector_once,
    run_collector_cycles,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading


class FakeDatabase:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection[Any], None, None]:
        yield self.conn
        self.conn.commit()


_GOOD_AT = datetime(2026, 11, 1, 11, 55, tzinfo=UTC)
_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _deepseek_collector(collector_id: str) -> CollectorConfig:
    return CollectorConfig(
        id=collector_id,
        label="DeepSeek",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=10,
    )


def _http_429(_req: HttpJsonRequest) -> dict[str, object]:
    raise HttpStatusError(429, "Too Many Requests")


def _http_500(_req: HttpJsonRequest) -> dict[str, object]:
    raise HttpStatusError(500, "boom")


def _seed_card(
    conn: psycopg.Connection[Any], card_id: str, *, value: str, updated_at: datetime
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plugin_stats (plugin, id, value, detail, status, updated_at, updated_by) "
            "VALUES ('meiva_usage', %s, %s, 'healthy', 'ok', %s, 'test')",
            (card_id, value, updated_at),
        )
    conn.commit()


def _card_row(conn: psycopg.Connection[Any], card_id: str) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value, detail, status, updated_at FROM plugin_stats "
            "WHERE plugin = 'meiva_usage' AND id = %s",
            (card_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _refresh_429(
    db_conn: psycopg.Connection[Any], tmp_path: Path, collector: CollectorConfig, now: datetime
) -> dict[str, CardReading]:
    return refresh_collector_once(
        collector,
        db=cast(Any, FakeDatabase(db_conn)),
        user_home=tmp_path,
        environ={"DEEPSEEK_API_KEY": "k"},
        now=now,
        platform="darwin",
        http_fetch=_http_429,
    )


def test_rate_limit_keeps_the_last_good_reading_and_its_age(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    collector = _deepseek_collector("rl-keep")
    _seed_card(db_conn, "rl-keep", value="$99.00", updated_at=_GOOD_AT)

    readings = _refresh_429(db_conn, tmp_path, collector, _NOW)

    reading = readings["rl-keep"]
    assert reading.rate_limited is True
    assert reading.value == "$99.00"
    assert reading.status == "warn"
    value, detail, status, updated_at = _card_row(db_conn, "rl-keep")
    assert (value, status) == ("$99.00", "warn")
    assert "HTTP 429" in detail
    assert "2026-11-01 11:55 UTC" in detail
    assert updated_at == _GOOD_AT


def test_a_second_rate_limit_still_reports_the_original_as_of(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    collector = _deepseek_collector("rl-twice")
    _seed_card(db_conn, "rl-twice", value="$99.00", updated_at=_GOOD_AT)

    _refresh_429(db_conn, tmp_path, collector, _NOW)
    _refresh_429(db_conn, tmp_path, collector, _NOW + timedelta(minutes=20))

    value, detail, status, updated_at = _card_row(db_conn, "rl-twice")
    assert (value, status) == ("$99.00", "warn")
    assert "2026-11-01 11:55 UTC" in detail
    assert updated_at == _GOOD_AT


def test_rate_limit_without_an_earlier_reading_writes_a_warning_placeholder(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    collector = _deepseek_collector("rl-empty")

    readings = _refresh_429(db_conn, tmp_path, collector, _NOW)

    assert readings["rl-empty"].rate_limited is True
    value, detail, status, _updated_at = _card_row(db_conn, "rl-empty")
    assert (value, status) == ("—", "warn")
    assert "rate limited" in detail


def test_a_good_probe_after_a_rate_limit_restores_the_card(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    collector = _deepseek_collector("rl-recover")
    _seed_card(db_conn, "rl-recover", value="$99.00", updated_at=_GOOD_AT)
    _refresh_429(db_conn, tmp_path, collector, _NOW)

    def ok_fetch(_req: HttpJsonRequest) -> dict[str, object]:
        return {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "42.50",
                    "granted_balance": "0.00",
                    "topped_up_balance": "42.50",
                }
            ],
        }

    readings = refresh_collector_once(
        collector,
        db=cast(Any, FakeDatabase(db_conn)),
        user_home=tmp_path,
        environ={"DEEPSEEK_API_KEY": "k"},
        now=_NOW + timedelta(minutes=30),
        platform="darwin",
        http_fetch=ok_fetch,
    )

    assert readings["rl-recover"].rate_limited is False
    value, _detail, status, updated_at = _card_row(db_conn, "rl-recover")
    assert "42.50" in value
    assert status == "ok"
    assert updated_at > _GOOD_AT


def test_other_probe_failures_still_overwrite_with_an_error_reading(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    collector = _deepseek_collector("rl-not")
    readings = refresh_collector_once(
        collector,
        db=cast(Any, FakeDatabase(db_conn)),
        user_home=tmp_path,
        environ={"DEEPSEEK_API_KEY": "k"},
        now=_NOW,
        platform="darwin",
        http_fetch=_http_500,
    )
    assert readings["rl-not"].rate_limited is False
    assert readings["rl-not"].status == "error"


def test_the_cycle_loop_backs_off_after_a_rate_limit_and_recovers(
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    emitted_events: EmittedEvents,
) -> None:
    collector = _deepseek_collector("loop-rl")
    cfg_dir = tmp_path / "configs" / "meiva_usage"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(UsageConfig(collectors=(collector,)).model_dump_json())

    class FakeMachine:
        def name(self) -> str:
            return "gateway"

    inputs = ScheduleInputs(
        database=cast(Any, lambda: FakeDatabase(db_conn)),
        producer=emitted_events.producer,
        image=LoadedCommit(source_root=tmp_path, sha="test-sha"),
        machine=cast(Any, FakeMachine()),
        home=tmp_path,
    )
    limited = {"loop-rl": CardReading(value="—", detail="", status="warn", rate_limited=True)}
    good = {"loop-rl": CardReading(value="1", detail="", status="ok")}
    script = iter([limited, limited, good, good, limited])
    sleeps: list[float] = []

    def scripted_refresh(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        return next(script)

    run_collector_cycles(
        inputs,
        refresh=scripted_refresh,
        sleep=sleeps.append,
        collector_id="loop-rl",
        stop_after_cycles=5,
        environ={"DEEPSEEK_API_KEY": "k"},
    )

    assert sleeps == [1200.0, 2400.0, 600.0, 600.0]
    attributes = list(emitted_events.attributes("plugin_collector_cycle"))
    assert [a["outcome"] for a in attributes] == [
        "rate_limited",
        "rate_limited",
        "ok",
        "ok",
        "rate_limited",
    ]
    assert attributes[0]["reason"] == "rate_limited"
    assert attributes[2]["reason"] == ""
