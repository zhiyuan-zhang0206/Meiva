from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import Database
from base.telemetry.metrics import usage
from cli.commands.management.metrics_legacy_usage import cmd_import_legacy_usage
from cli.database import operator_database_factory

_LEDGER_START = date(2026, 9, 26)


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('legacy') RETURNING id").fetchone()
    assert row is not None
    db.execute(
        "INSERT INTO agents_meta (id, status, spawner, born_spawner) "
        "VALUES (%s, 'idling', 'user', 'user')",
        (row[0],),
    )
    return int(row[0])


def _day(
    db: psycopg.Connection,
    agent: int,
    day: date,
    model: str,
    *,
    calls: int = 2,
    cost: float = 0.5,
    sub_cost: float = 0.0,
    unpriced: int = 0,
    sub_unpriced: int = 0,
) -> None:
    db.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
        "tokens_out, tokens_cached, tokens_reasoning, cost_usd, cost_usd_subscription, "
        "costed_calls, unpriced_calls, unpriced_calls_subscription) "
        "VALUES (%s, %s, %s, %s, 100, 20, 60, 5, %s, %s, %s, %s, %s)",
        (agent, day, model, calls, cost, sub_cost, calls - unpriced, unpriced, sub_unpriced),
    )


def _ledger_call(db: psycopg.Connection, agent: int, day: date, calls: int = 3) -> None:
    db.execute(
        "INSERT INTO llm_usage_ledger (call_id, occurred_at, agent_id, machine, cluster, billing, "
        "usage_kind, model, calls, complete, tokens_in, tokens_out, cost_usd, price_snapshot, "
        "price_source) VALUES (%s, %s, %s, 'm', 'c', 'api', 'turn', 'm', %s, true, 1, 1, 0.1, %s, "
        "'live')",
        (
            f"api:{agent}:{day}",
            datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            agent,
            calls,
            Jsonb({}),
        ),
    )


def _run(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    apply: bool,
) -> tuple[int, dict[str, Any]]:
    db_conn.commit()
    factory = operator_database_factory(
        local_host=lambda: "127.0.0.1",
        home=tmp_path,
        open_database=lambda _cfg, _gate, _direct: database,
    )
    rc = cmd_import_legacy_usage(factory, cluster="test", apply=apply, batch_size=2)
    return rc, json.loads(capsys.readouterr().out)


def _seed_days_around_the_ledger_start(db_conn: psycopg.Connection) -> tuple[int, date]:
    agent = _agent(db_conn)
    old = _LEDGER_START - timedelta(days=3)
    _day(db_conn, agent, old, "api-model", calls=4, cost=1.25)
    _day(db_conn, agent, old, "sub-model", calls=3, cost=2.0, sub_cost=2.0)
    _day(db_conn, agent, old + timedelta(days=1), "api-model", calls=2, cost=0.0, unpriced=2)
    _day(db_conn, agent, old + timedelta(days=1), "idle", calls=0)
    _day(db_conn, agent, old + timedelta(days=2), "part", calls=5, cost=0.4, unpriced=2)
    _day(db_conn, agent, _LEDGER_START, "api-model", calls=3, cost=0.3)
    _ledger_call(db_conn, agent, _LEDGER_START)
    return agent, old


def test_the_dry_run_reports_the_scope_and_writes_nothing(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, old = _seed_days_around_the_ledger_start(db_conn)

    rc, dry = _run(db_conn, database, tmp_path, capsys, apply=False)

    assert rc == 0
    assert dry["mode"] == "dry-run"
    assert (dry["cutoff_day"], dry["first_day"], dry["last_day"]) == (
        _LEDGER_START.isoformat(),
        old.isoformat(),
        (old + timedelta(days=2)).isoformat(),
    )
    assert (dry["rows"], dry["skipped_zero_call_rows"], dry["partly_priced_rows"]) == (4, 1, 1)
    assert (dry["unpriced_rows"], dry["unpriced_calls"]) == (1, 2)
    assert dry["by_billing"]["api"]["calls"] == 11
    assert dry["by_billing"]["subscription"]["cost_usd"] == "2.0"
    assert dry["overlap_days"] == [
        {"day": _LEDGER_START.isoformat(), "daily_calls": 3, "ledger_calls": 3}
    ]
    assert db_conn.execute("SELECT count(*) FROM llm_usage_ledger").fetchone() == (1,)


def test_apply_imports_once_and_keeps_the_stored_costs(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent, _ = _seed_days_around_the_ledger_start(db_conn)

    rc, applied = _run(db_conn, database, tmp_path, capsys, apply=True)
    assert (rc, applied["import"]) == (0, {"inserted": 4, "existing": 0})
    assert applied["ledger_after"]["subscription"]["calls"] == 3
    rc, again = _run(db_conn, database, tmp_path, capsys, apply=True)
    assert (rc, again["import"]) == (0, {"inserted": 0, "existing": 4})

    rows = db_conn.execute(
        "SELECT call_id, billing, usage_kind, calls, cache_read, cost_usd, price_source "
        "FROM llm_usage_ledger WHERE call_id LIKE 'legacy-daily:%' ORDER BY call_id"
    ).fetchall()
    assert [(r[0].split(":", 2)[2], r[1], r[3], r[5], r[6]) for r in rows] == [
        (f"{agent}:api-model", "api", 4, 1.25, "legacy"),
        (f"{agent}:sub-model", "subscription", 3, 2.0, "legacy"),
        (f"{agent}:api-model", "api", 2, None, None),
        (f"{agent}:part", "api", 5, Decimal("0.4"), "legacy"),
    ]
    assert db_conn.execute(
        "SELECT call_id FROM llm_usage_ledger WHERE NOT complete"
    ).fetchall() == [(f"legacy-daily:{_LEDGER_START - timedelta(days=1)}:{agent}:part",)]
    assert {r[2] for r in rows} == {"legacy_daily"}
    assert {r[4] for r in rows} == {60}


def test_the_imported_days_count_in_a_lifetime_usage_report(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent, _ = _seed_days_around_the_ledger_start(db_conn)
    _run(db_conn, database, tmp_path, capsys, apply=True)

    report = usage.usage_report(
        db_conn, roots=[agent], lineage="self", start=None, end=datetime.now(UTC)
    )

    assert report["totals"]["calls"] == 3 + 4 + 3 + 2 + 5
    assert report["totals"]["unpriced_calls"] == 2


def test_a_row_that_mixes_billings_blocks_the_import_and_is_listed(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = _agent(db_conn)
    old = _LEDGER_START - timedelta(days=2)
    _day(db_conn, agent, old, "fine", cost=1.0)
    _day(db_conn, agent, old, "mixed", calls=5, cost=3.0, sub_cost=1.0)
    _ledger_call(db_conn, agent, _LEDGER_START)

    rc, report = _run(db_conn, database, tmp_path, capsys, apply=True)

    assert rc == 2
    assert [row["model"] for row in report["mixed_billing_rows"]] == ["mixed"]
    assert "import" not in report
    assert db_conn.execute("SELECT count(*) FROM llm_usage_ledger").fetchone() == (1,)


def test_an_empty_ledger_has_no_start_to_cut_at(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    db_conn.commit()
    factory = operator_database_factory(
        local_host=lambda: "127.0.0.1",
        home=tmp_path,
        open_database=lambda _cfg, _gate, _direct: database,
    )
    with pytest.raises(ValueError, match="no per-call report"):
        cmd_import_legacy_usage(factory, cluster="test", apply=False, batch_size=10)
