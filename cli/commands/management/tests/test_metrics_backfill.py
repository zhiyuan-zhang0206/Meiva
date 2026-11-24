from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import Database
from base.telemetry.delivery.mirror import event_id as mirror_event_id
from base.telemetry.metrics import observed_metrics
from cli.commands.management.metrics_backfill import (
    cmd_backfill_usage_ledger,
    cmd_metrics_backfill_unpriced,
    cmd_reconcile_usage_ledger,
)
from cli.database import operator_database_factory


def test_cmd_metrics_backfill_unpriced_dry_run_and_apply_json(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_conn.autocommit = True
    row = db_conn.execute(
        "INSERT INTO agents (label) VALUES ('cli-backfill') RETURNING id"
    ).fetchone()
    assert row is not None
    agent_id = int(row[0])

    ts = datetime(2026, 11, 21, 19, 0, tzinfo=UTC)
    obs = observed_metrics.observe_row(
        {
            "id": mirror_event_id("cli-backfill", int(ts.timestamp() * 1_000_000_000)),
            "agent_id": agent_id,
            "ts": ts,
            "category": "telemetry",
            "event_name": "llm_usage",
            "attributes": {
                "model": "gemini-4-neon-high",
                "in_total": 50_000,
                "out_total": 5_000,
                "reasoning": 0,
                "source": "antigravity",
                "unpriced": 1,
            },
        }
    )
    assert obs is not None
    assert observed_metrics.write_observations([obs], db=db_conn) == 1

    factory = operator_database_factory(
        local_host=lambda: "127.0.0.1",
        home=tmp_path,
        open_database=lambda _cfg, _gate, _direct: database,
    )

    rc = cmd_metrics_backfill_unpriced(factory, today=date(2026, 11, 22), apply=False, as_json=True)
    assert rc == 0
    dry_payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert dry_payload["dry_run"] is True
    assert dry_payload["observations_priced"] == 1

    rc = cmd_metrics_backfill_unpriced(factory, today=date(2026, 11, 22), apply=True, as_json=False)
    assert rc == 0
    text = capsys.readouterr().out
    assert "mode:                applied" in text
    assert "gemini-4-neon-high -> gemini-4-neon" in text

    rc = cmd_metrics_backfill_unpriced(factory, today=date(2026, 11, 22), apply=True, as_json=True)
    assert rc == 0
    rerun_payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rerun_payload["observations_scanned"] == 0
    assert rerun_payload["observations_priced"] == 0


def test_reconcile_cli_exits_nonzero_for_a_record_only_difference(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ts = datetime(2026, 11, 21, tzinfo=UTC)
    db_conn.execute(
        """INSERT INTO telemetry_events
           (event_uid, ts, machine, cluster, process, category, event_name, level, source, attributes)
           VALUES (1, %s, 'test', 'test', 'gateway', 'telemetry', 'llm_usage', 'info', 'system', %s)""",
        (
            ts,
            Jsonb(
                {"call_id": "source-key", "model": "deepseek-flash", "in_total": 2, "out_total": 1}
            ),
        ),
    )
    db_conn.commit()
    factory = operator_database_factory(
        local_host=lambda: "127.0.0.1",
        home=tmp_path,
        open_database=lambda _cfg, _gate, _direct: database,
    )
    start, end = ts.date(), date(2026, 11, 22)
    assert cmd_backfill_usage_ledger(factory, start=start, end=end, batch_size=1) == 0
    capsys.readouterr()
    assert cmd_reconcile_usage_ledger(factory, start=start, end=end, batch_size=1) == 0
    assert json.loads(capsys.readouterr().out)["matches"] is True

    db_conn.execute(
        "UPDATE llm_usage_ledger SET call_id = 'ledger-key' WHERE call_id = 'source-key'"
    )
    db_conn.commit()
    assert cmd_reconcile_usage_ledger(factory, start=start, end=end, batch_size=1) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mismatched_groups"] == 0
    assert report["matches"] is False
    assert report["record_differences"] == [
        {"call_id": "ledger-key", "fields": [], "kind": "ledger_only"},
        {"call_id": "source-key", "fields": [], "kind": "source_only"},
    ]
