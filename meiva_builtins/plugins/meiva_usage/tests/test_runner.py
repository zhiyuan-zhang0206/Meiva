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
from meiva_builtins.plugins.meiva_usage.probes import HttpJsonRequest
from meiva_builtins.plugins.meiva_usage.runner import refresh_collector_once, run_from_schedule


class FakeDatabase:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    @contextmanager
    def write_transaction(self) -> Generator[psycopg.Connection[Any], None, None]:
        yield self.conn
        self.conn.commit()


def test_refresh_collector_once_success(db_conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="test-deepseek",
        label="DeepSeek Balance",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
    )
    environ = {"DEEPSEEK_API_KEY": "test-key-123"}
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def mock_fetch(req: HttpJsonRequest) -> dict[str, object]:
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

    reading = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    assert reading.status == "ok"
    assert "42.50" in reading.value
    assert "USD" in reading.value

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, detail, status, updated_at FROM plugin_stats WHERE plugin = %s AND id = %s",
            ("meiva_usage", "test-deepseek"),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == reading.value
        assert row[2] == "ok"


def test_refresh_collector_once_suppresses_fresh_failure(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="test-failing",
        label="Failing Collector",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
    )
    environ = {"DEEPSEEK_API_KEY": "test-key-123"}
    now_fresh = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plugin_stats (plugin, id, value, detail, status, updated_at, updated_by) "
            "VALUES ('meiva_usage', 'test-failing', '$99.00', 'healthy', 'ok', %s, 'test')",
            (now_fresh - timedelta(minutes=5),),
        )
    db_conn.commit()

    def failing_fetch(req: HttpJsonRequest) -> dict[str, object]:
        raise RuntimeError("network down")

    reading = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now_fresh,
        platform="darwin",
        http_fetch=failing_fetch,
    )

    assert reading.value == "$99.00"
    assert reading.status == "ok"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, status FROM plugin_stats WHERE plugin = 'meiva_usage' AND id = 'test-failing'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "$99.00"
        assert row[1] == "ok"


def test_refresh_collector_once_writes_stale_failure(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="test-stale",
        label="Stale Collector",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
    )
    environ = {"DEEPSEEK_API_KEY": "test-key-123"}
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plugin_stats (plugin, id, value, detail, status, updated_at, updated_by) "
            "VALUES ('meiva_usage', 'test-stale', '$50.00', 'healthy', 'ok', %s, 'test')",
            (now - timedelta(minutes=45),),
        )
    db_conn.commit()

    def failing_fetch(req: HttpJsonRequest) -> dict[str, object]:
        raise RuntimeError("api rate limited")

    reading = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=failing_fetch,
    )
    assert reading.status == "error"
    assert "api rate limited" in reading.detail

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, detail, status FROM plugin_stats WHERE plugin = 'meiva_usage' AND id = 'test-stale'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "$50.00"
        assert row[2] == "error"


def test_run_from_schedule_single_cycle(db_conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="scheduled-coll",
        label="Scheduled Collector",
        provider="deepseek",
        machine="company-air",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=1,
    )
    usage_cfg = UsageConfig(collectors=(collector,))
    cfg_dir = tmp_path / "configs" / "meiva_usage"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(usage_cfg.model_dump_json(indent=2))

    class FakeMachine:
        def name(self) -> str:
            return "company-air"

    inputs = ScheduleInputs(
        database=cast(Any, lambda: db),
        producer=cast(Any, lambda: None),
        image=LoadedCommit(source_root=tmp_path, sha="test-sha"),
        machine=cast(Any, FakeMachine()),
        home=tmp_path,
    )

    environ = {
        "DEEPSEEK_API_KEY": "test-key-123",
        "MEIVA_MACHINE_SERVE_GATEWAY": "true",
    }

    def mock_fetch(req: HttpJsonRequest) -> dict[str, object]:
        return {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "88.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "88.00",
                }
            ],
        }

    run_from_schedule(
        inputs,
        collector_id="scheduled-coll",
        stop_after_cycles=1,
        environ=environ,
        http_fetch=mock_fetch,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, updated_by, value, status FROM plugin_stats WHERE plugin = 'meiva_usage' AND id = 'scheduled-coll'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "scheduled-coll"
        assert row[1] == "company-air"
        assert "88.00" in row[2]
        assert "USD" in row[2]
        assert row[3] == "ok"


def test_run_from_schedule_split_runner_cluster_projection(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="split-coll",
        label="Split Collector",
        provider="deepseek",
        machine="split-runner-01",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=1,
    )
    empty_cfg = UsageConfig(collectors=())
    cfg_dir = tmp_path / "configs" / "meiva_usage"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(empty_cfg.model_dump_json(indent=2))

    class FakeMachine:
        def name(self) -> str:
            return "split-runner-01"

    inputs = ScheduleInputs(
        database=cast(Any, lambda: db),
        producer=cast(Any, lambda: None),
        image=LoadedCommit(source_root=tmp_path, sha="test-sha"),
        machine=cast(Any, FakeMachine()),
        home=tmp_path,
    )

    environ = {
        "DEEPSEEK_API_KEY": "test-key-456",
        "MEIVA_PLUGIN_CLUSTER_CONFIG": json.dumps(
            {
                "meiva_usage": {
                    "collectors": [collector.model_dump(mode="json")],
                }
            }
        ),
    }

    def mock_fetch(req: HttpJsonRequest) -> dict[str, object]:
        return {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "55.00",
                    "granted_balance": "0.00",
                    "topped_up_balance": "55.00",
                }
            ],
        }

    run_from_schedule(
        inputs,
        collector_id="split-coll",
        stop_after_cycles=1,
        environ=environ,
        http_fetch=mock_fetch,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, updated_by, value, status FROM plugin_stats WHERE plugin = 'meiva_usage' AND id = 'split-coll'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "split-coll"
        assert row[1] == "split-runner-01"
        assert "55.00" in row[2]
        assert "USD" in row[2]
        assert row[3] == "ok"


def test_schedules_face_filtering_and_fingerprint() -> None:
    from meiva_builtins.plugins.meiva_usage.schedules import contribute

    c_active = CollectorConfig(
        id="coll-active",
        label="Active",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=5,
        enabled=True,
    )
    c_disabled = CollectorConfig(
        id="coll-disabled",
        label="Disabled",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=10,
        enabled=False,
    )
    cfg1 = UsageConfig(collectors=(c_active, c_disabled))
    specs1 = contribute(cfg1)
    assert len(specs1) == 1
    assert specs1[0].name == "meiva_usage:coll-active"
    script1 = specs1[0].script
    assert "# collector_spec:" in script1
    assert '"interval_minutes":5' in script1

    c_active_bumped = CollectorConfig(
        id="coll-active",
        label="Active",
        provider="deepseek",
        machine="gateway",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=15,
        enabled=True,
    )
    cfg2 = UsageConfig(collectors=(c_active_bumped,))
    specs2 = contribute(cfg2)
    assert len(specs2) == 1
    script2 = specs2[0].script
    assert '"interval_minutes":15' in script2
    assert script1 != script2
