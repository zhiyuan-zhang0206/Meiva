from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import Database
from base.telemetry.delivery.mirror import event_id as mirror_event_id
from base.telemetry.metrics import observed_metrics
from cli.commands.management.metrics_backfill import cmd_metrics_backfill_unpriced
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
