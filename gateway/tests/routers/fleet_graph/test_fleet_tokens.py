from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gateway.routers.fleet.fleet_tokens import agent_tokens

_NOW = datetime(2026, 6, 30, 15, 30, tzinfo=UTC)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('a') RETURNING id").fetchone()
    assert row is not None
    return int(row[0])


def _usage(
    db: psycopg.Connection, agent: int, at: datetime, tokens_in: int, tokens_out: int
) -> None:
    db.execute(
        """INSERT INTO llm_usage_ledger
           (call_id, occurred_at, agent_id, machine, cluster, billing, usage_kind, model,
            calls, complete, tokens_in, tokens_out, price_snapshot)
           VALUES (%s, %s, %s, 'm', 'c', 'api', 'test', 'm', 1, TRUE, %s, %s, '{}')""",
        (str(uuid.uuid4()), at, agent, tokens_in, tokens_out),
    )


def _brute(db: psycopg.Connection, agent: int, start: datetime | None) -> tuple[float, float]:
    row = db.execute(
        "SELECT COALESCE(sum(tokens_in), 0), COALESCE(sum(tokens_out), 0) FROM llm_usage_ledger "
        "WHERE agent_id = %s AND occurred_at < %s AND (%s::timestamptz IS NULL OR occurred_at >= %s)",
        (agent, _NOW, start, start),
    ).fetchone()
    assert row is not None
    return float(row[0]), float(row[1])


@pytest.fixture
def agent(db: psycopg.Connection) -> int:
    agent = _agent(db)
    for age_hours in range(1, 24 * 40, 7):
        _usage(db, agent, _NOW - timedelta(hours=age_hours), 100 + age_hours, 10 + age_hours % 13)
    _usage(db, agent, _NOW - timedelta(minutes=5), 7, 3)
    _usage(db, agent, _NOW, 222, 111)
    return agent


@pytest.mark.parametrize(
    "start",
    [
        _NOW - timedelta(minutes=5),
        _NOW - timedelta(hours=1),
        _NOW - timedelta(hours=24),
        _NOW - timedelta(hours=72),
        _NOW - timedelta(days=7),
        datetime(2026, 6, 27, tzinfo=UTC),
        datetime(2026, 6, 25, 3, 17, tzinfo=UTC),
        None,
    ],
)
def test_every_window_equals_the_sum_of_its_raw_rows(
    db: psycopg.Connection, agent: int, start: datetime | None
) -> None:
    tokens = agent_tokens(db, now=_NOW, win_start=start)[agent]

    in_window, out_window = _brute(db, agent, start)
    in_retained, out_retained = _brute(db, agent, _NOW - timedelta(days=7))
    assert (tokens.in_window, tokens.out_window) == (in_window, out_window)
    assert (tokens.in_retained, tokens.out_retained) == (in_retained, out_retained)


def test_an_agent_without_usage_has_no_entry(db: psycopg.Connection, agent: int) -> None:
    assert agent_tokens(db, now=_NOW, win_start=None).get(_agent(db)) is None
