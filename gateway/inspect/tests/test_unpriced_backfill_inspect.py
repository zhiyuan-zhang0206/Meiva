from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg

from base.db.rows import IdRow, query_one
from base.telemetry.delivery.mirror import event_id as mirror_event_id
from base.telemetry.event_store import event_uid
from gateway.inspect._metrics import read_cost
from services.upkeep.events_maintenance.unpriced_backfill import load_price_book
from services.upkeep.events_maintenance.usage_ledger_backfill import backfill_usage_ledger


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


def test_inspector_reads_historical_usage_after_ledger_backfill(
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

    before = read_cost(
        db_conn,
        agent_id,
        datetime(2026, 11, 20, tzinfo=UTC),
        datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    assert before.llm_calls == 0
    assert before.cost_usd_subscription == 0

    report = backfill_usage_ledger(
        db_conn,
        start=datetime(2026, 11, 20, tzinfo=UTC),
        end=datetime(2026, 11, 22, tzinfo=UTC),
        batch_size=2,
        prices=load_price_book(),
    )
    assert (report.scanned, report.inserted, report.catalog_priced) == (2, 2, 1)

    after = read_cost(
        db_conn,
        agent_id,
        datetime(2026, 11, 20, tzinfo=UTC),
        datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    assert after.llm_calls == 2
    assert after.tokens_in == 200_000
    assert after.cost_usd_api == 0
    assert after.unpriced_calls == 0
    assert after.unpriced_calls_subscription == 0
    assert after.unpriced_calls_api == 0
    assert after.cost_usd_subscription > 0.4321
