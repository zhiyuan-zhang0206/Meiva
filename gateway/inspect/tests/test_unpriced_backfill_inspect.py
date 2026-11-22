from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from base.db.rows import IdRow, query_one
from base.telemetry.delivery.mirror import event_id as mirror_event_id
from base.telemetry.event_store import event_uid
from base.telemetry.metrics import observed_metrics
from gateway.inspect._metrics import read_snapshot
from services.upkeep.events_maintenance import unpriced_backfill

_CUTOVER = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)


def _seed_usage(
    conn: psycopg.Connection,
    *,
    agent_id: int,
    occurred_at: datetime,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float | None,
    source: str,
    tag: str,
) -> None:
    attrs: dict[str, object] = {
        "model": model,
        "in_total": tokens_in,
        "out_total": tokens_out,
        "reasoning": 0,
        "source": source,
    }
    if cost_usd is not None:
        attrs["cost_usd"] = cost_usd
    else:
        attrs["unpriced"] = 1
    ts_ns = int(occurred_at.timestamp() * 1_000_000_000)
    sid = mirror_event_id(f"{tag}:{agent_id}:{model}", ts_ns)
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (event_uid(sid), occurred_at, agent_id, json.dumps(attrs)),
    )
    obs = observed_metrics.observe_row(
        {
            "id": sid,
            "agent_id": agent_id,
            "ts": occurred_at,
            "category": "telemetry",
            "event_name": "llm_usage",
            "attributes": attrs,
        }
    )
    assert obs is not None
    assert observed_metrics.write_observations([obs], db=conn) == 1


def test_inspector_snapshot_unpriced_drops_to_zero_after_backfill(
    db_conn: psycopg.Connection,
) -> None:
    db_conn.autocommit = True
    row = query_one(
        db_conn, IdRow, "INSERT INTO agents (label) VALUES ('insp-backfill') RETURNING id"
    )
    assert row is not None
    agent_id = row.id

    ts = datetime(2026, 11, 21, 18, 30, tzinfo=UTC)
    _seed_usage(
        db_conn,
        agent_id=agent_id,
        occurred_at=ts,
        model="gemini-4-neon-high",
        tokens_in=100_000,
        tokens_out=10_000,
        cost_usd=None,
        source="antigravity",
        tag="neon-unpriced",
    )
    _seed_usage(
        db_conn,
        agent_id=agent_id,
        occurred_at=ts + timedelta(minutes=5),
        model="gemini-4-neon",
        tokens_in=100_000,
        tokens_out=10_000,
        cost_usd=0.4321,
        source="antigravity",
        tag="neon-already-priced",
    )

    before = read_snapshot(
        db_conn,
        agent_id,
        datetime(2026, 11, 20, tzinfo=UTC),
        datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
        _CUTOVER,
    )
    assert before.cost is not None
    assert before.cost.unpriced_calls == 1
    assert before.cost.unpriced_calls_subscription == 1
    assert before.cost.cost_usd_subscription == pytest.approx(0.4321)

    report = unpriced_backfill.run_unpriced_backfill(
        db_conn, today=date(2026, 11, 22), dry_run=False
    )
    assert report.observations_priced == 1

    after = read_snapshot(
        db_conn,
        agent_id,
        datetime(2026, 11, 20, tzinfo=UTC),
        datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
        _CUTOVER,
    )
    assert after.cost is not None
    assert after.cost.unpriced_calls == 0
    assert after.cost.unpriced_calls_subscription == 0
    assert after.cost.unpriced_calls_api == 0
    assert after.cost.cost_usd_subscription > 0.4321
