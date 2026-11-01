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

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    reading = readings[collector.id]
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

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now_fresh,
        platform="darwin",
        http_fetch=failing_fetch,
    )
    reading = readings[collector.id]

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

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=failing_fetch,
    )
    reading = readings[collector.id]
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


def test_refresh_collector_once_openai(db_conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="test-openai",
        label="OpenAI Spend",
        provider="openai",
        machine="gateway",
        key_env="OPENAI_ADMIN_KEY",
        known_balance_usd=100.0,
        balance_as_of="2026-10-15",
    )
    environ = {"OPENAI_ADMIN_KEY": "sk-test-admin"}
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    today_epoch = int(datetime(2026, 11, 1, 0, 0, tzinfo=UTC).timestamp())

    def mock_fetch(_req: HttpJsonRequest) -> dict[str, object]:
        return {
            "data": [
                {
                    "start_time": today_epoch,
                    "results": [{"amount": {"value": 12.50, "currency": "usd"}}],
                }
            ],
            "has_more": False,
        }

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    reading = readings[collector.id]
    assert reading.status == "ok"
    assert reading.value == "$87.50 left"
    assert "spent $12.50 since 2026-10-15" in reading.detail

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, detail, status, updated_at FROM plugin_stats WHERE plugin = %s AND id = %s",
            ("meiva_usage", "test-openai"),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "$87.50 left"
        assert row[2] == "ok"


def test_refresh_collector_once_anthropic(db_conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="test-anthropic",
        label="Anthropic Costs",
        provider="anthropic",
        machine="gateway",
        key_env="ANTHROPIC_ADMIN_KEY",
    )
    environ = {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-test"}
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def mock_fetch(_req: HttpJsonRequest) -> dict[str, object]:
        return {
            "data": [
                {
                    "starting_at": "2026-11-01T00:00:00Z",
                    "results": [{"amount": "3450", "currency": "USD"}],
                }
            ],
            "has_more": False,
        }

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ=environ,
        now=now,
        platform="darwin",
        http_fetch=mock_fetch,
    )
    reading = readings[collector.id]
    assert reading.status == "ok"
    assert reading.value == "$34.50 (30d)"
    assert "today $34.50 · 30d $34.50" in reading.detail

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT value, detail, status, updated_at FROM plugin_stats WHERE plugin = %s AND id = %s",
            ("meiva_usage", "test-anthropic"),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "$34.50 (30d)"
        assert row[2] == "ok"


def _assert_ag_phase1_initial_write(
    readings: dict[str, Any],
    db_conn: psycopg.Connection[Any],
) -> None:
    assert len(readings) == 2
    assert readings["ag-cluster-gemini"].status == "ok"
    assert readings["ag-cluster-gemini"].value == "7d 82% · 5h 76% left"
    assert readings["ag-cluster-claude-gpt"].status == "warn"
    assert readings["ag-cluster-claude-gpt"].value == "7d 1% · 5h 6% left"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, value, status FROM plugin_stats "
            "WHERE plugin = 'meiva_usage' AND id IN ('ag-cluster-gemini', 'ag-cluster-claude-gpt') "
            "ORDER BY id"
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert rows[0] == ("ag-cluster-claude-gpt", "7d 1% · 5h 6% left", "warn")
        assert rows[1] == ("ag-cluster-gemini", "7d 82% · 5h 76% left", "ok")


def _assert_ag_phase2_fresh_failure(
    readings2: dict[str, Any],
    db_conn: psycopg.Connection[Any],
) -> None:
    assert readings2["ag-cluster-gemini"].status == "ok"
    assert readings2["ag-cluster-gemini"].value == "7d 80% · 5h 75% left"
    assert readings2["ag-cluster-claude-gpt"].status == "warn"
    assert readings2["ag-cluster-claude-gpt"].value == "7d 1% · 5h 6% left"
    assert "probe error suppressed" in readings2["ag-cluster-claude-gpt"].detail

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, value, status FROM plugin_stats "
            "WHERE plugin = 'meiva_usage' AND id = 'ag-cluster-claude-gpt'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[1] == "7d 1% · 5h 6% left"
        assert row[2] == "warn"


def _assert_ag_phase3_stale_failure(
    readings3: dict[str, Any],
    db_conn: psycopg.Connection[Any],
) -> None:
    assert readings3["ag-cluster-claude-gpt"].status == "error"
    assert readings3["ag-cluster-claude-gpt"].value == "7d 1% · 5h 6% left"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, value, status FROM plugin_stats "
            "WHERE plugin = 'meiva_usage' AND id = 'ag-cluster-claude-gpt'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[1] == "7d 1% · 5h 6% left"
        assert row[2] == "error"


def test_refresh_collector_once_antigravity_writes_both_cards_and_handles_hysteresis(
    db_conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    db = FakeDatabase(db_conn)
    collector = CollectorConfig(
        id="ag-cluster",
        label="Cluster AG",
        provider="antigravity",
        machine="gateway",
    )

    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "token": {
                    "access_token": "valid-token",
                    "refresh_token": "valid-refresh",
                }
            }
        ),
        encoding="utf-8",
    )

    t0 = datetime.now(tz=UTC)

    def healthy_fetch(req: HttpJsonRequest) -> dict[str, object]:
        return {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {"bucketId": "gemini-weekly", "remainingFraction": 0.8156919},
                            {"bucketId": "gemini-5h", "remainingFraction": 0.7631871},
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {"bucketId": "3p-weekly", "remainingFraction": 0.00902968},
                            {"bucketId": "3p-5h", "remainingFraction": 0.0563868},
                        ],
                    },
                ]
            }
        }

    readings = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ={},
        now=t0,
        platform="darwin",
        http_fetch=healthy_fetch,
    )
    _assert_ag_phase1_initial_write(readings, db_conn)

    t1_fresh = t0 + timedelta(minutes=5)

    def partial_fetch(_req: HttpJsonRequest) -> dict[str, object]:
        return {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-weekly", "remainingFraction": 0.80},
                        {"bucketId": "gemini-5h", "remainingFraction": 0.75},
                    ],
                }
            ]
        }

    readings2 = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ={},
        now=t1_fresh,
        platform="darwin",
        http_fetch=partial_fetch,
    )
    _assert_ag_phase2_fresh_failure(readings2, db_conn)

    t2_stale = t0 + timedelta(minutes=40)
    readings3 = refresh_collector_once(
        collector,
        db=cast(Any, db),
        user_home=tmp_path,
        environ={},
        now=t2_stale,
        platform="darwin",
        http_fetch=partial_fetch,
    )
    _assert_ag_phase3_stale_failure(readings3, db_conn)
