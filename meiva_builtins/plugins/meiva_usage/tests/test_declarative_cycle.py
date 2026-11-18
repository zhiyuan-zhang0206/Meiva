# meiva:note
# Declarative cycle pacing: `not_before` holds fires only during active rate-limit backoff.
# meiva:link meiva_builtins/plugins/meiva_usage/README.md

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg

from base.daemon.schedules.inputs import ScheduleInputs
from base.native_process.loaded_commit import LoadedCommit
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.runner import run_declarative_cycle
from meiva_builtins.plugins.meiva_usage.types import CardReading


class _FakeDatabase:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection[Any], None, None]:
        yield self.conn
        self.conn.commit()


def _declarative_harness(
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    *,
    collector_id: str = "paced",
    interval_minutes: int = 1,
) -> tuple[Path, dict[str, Any]]:
    collector = CollectorConfig(
        id=collector_id,
        label="Paced",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=interval_minutes,
    )
    cfg_dir = tmp_path / "configs" / "meiva_usage"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        UsageConfig(collectors=(collector,)).model_dump_json(indent=2)
    )

    class FakeMachine:
        def name(self) -> str:
            return "gateway"

    inputs = ScheduleInputs(
        database=cast(Any, lambda: _FakeDatabase(db_conn)),
        producer=cast(Any, lambda: None),
        image=LoadedCommit(source_root=tmp_path, sha="test-sha"),
        machine=cast(Any, FakeMachine()),
        home=tmp_path,
    )
    state_path = tmp_path / "schedule-workspace" / "state.json"
    common: dict[str, Any] = {
        "borrowed": inputs,
        "collector_id": collector_id,
        "state_path": state_path,
        "environ": {"DEEPSEEK_API_KEY": "test"},
        "user_home": tmp_path,
        "platform": "darwin",
    }
    return state_path, common


def test_declarative_cycle_persists_rate_limit_backoff(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    state_path, common = _declarative_harness(db_conn, tmp_path, interval_minutes=1)
    first = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    calls: list[str] = []

    def limited(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("limited")
        return {
            "paced": CardReading(
                value="—", detail="provider limited", status="warn", rate_limited=True
            )
        }

    def recovered(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("ok")
        return {"paced": CardReading(value="$1", detail="fresh", status="ok")}

    run_declarative_cycle(**common, clock=lambda: first, refresh=limited)
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 1,
        "not_before": "2026-11-17T00:02:00+00:00",
    }

    run_declarative_cycle(
        **common,
        clock=lambda: first + timedelta(seconds=60),
        refresh=limited,
    )
    assert calls == ["limited"]

    second_limit_at = first + timedelta(seconds=120)
    run_declarative_cycle(**common, clock=lambda: second_limit_at, refresh=limited)
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 2,
        "not_before": "2026-11-17T00:06:00+00:00",
    }

    run_declarative_cycle(
        **common,
        clock=lambda: second_limit_at + timedelta(seconds=239),
        refresh=limited,
    )
    assert calls == ["limited", "limited"]

    recovered_at = second_limit_at + timedelta(seconds=240)
    run_declarative_cycle(**common, clock=lambda: recovered_at, refresh=recovered)
    assert calls == ["limited", "limited", "ok"]
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 0,
        "not_before": None,
    }

    run_declarative_cycle(
        **common,
        clock=lambda: recovered_at + timedelta(seconds=5),
        refresh=recovered,
    )
    assert calls == ["limited", "limited", "ok", "ok"]


def test_declarative_cycle_does_not_hold_normal_or_error_cycles(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    state_path, common = _declarative_harness(db_conn, tmp_path, interval_minutes=30)
    t0 = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    calls: list[str] = []

    def ok_refresh(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("ok")
        return {"paced": CardReading(value="$1", detail="fresh", status="ok")}

    def error_refresh(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("error")
        return {"paced": CardReading(value="—", detail="expired token", status="error")}

    def raising_refresh(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("raised")
        raise RuntimeError("boom")

    run_declarative_cycle(**common, clock=lambda: t0, refresh=ok_refresh)
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 0,
        "not_before": None,
    }

    run_declarative_cycle(
        **common,
        clock=lambda: t0 + timedelta(seconds=5),
        refresh=error_refresh,
    )
    assert calls == ["ok", "error"]
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 0,
        "not_before": None,
    }

    run_declarative_cycle(
        **common,
        clock=lambda: t0 + timedelta(seconds=8),
        refresh=raising_refresh,
    )
    assert calls == ["ok", "error", "raised"]
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 0,
        "not_before": None,
    }

    run_declarative_cycle(
        **common,
        clock=lambda: t0 + timedelta(seconds=10),
        refresh=ok_refresh,
    )
    assert calls == ["ok", "error", "raised", "ok"]


def test_declarative_cycle_ignores_stale_not_before_when_rate_limit_count_is_zero(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    state_path, common = _declarative_harness(db_conn, tmp_path, interval_minutes=30)
    t0 = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "consecutive_rate_limits": 0,
                "not_before": (t0 + timedelta(minutes=25)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def ok_refresh(*_args: Any, **_kwargs: Any) -> dict[str, CardReading]:
        calls.append("ok")
        return {"paced": CardReading(value="$1", detail="fresh", status="ok")}

    run_declarative_cycle(**common, clock=lambda: t0, refresh=ok_refresh)
    assert calls == ["ok"]
    assert json.loads(state_path.read_text()) == {
        "consecutive_rate_limits": 0,
        "not_before": None,
    }
