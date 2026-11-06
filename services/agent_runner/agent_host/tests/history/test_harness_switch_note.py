import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.db.transaction import async_write_transaction
from services.agent_runner.agent_host import health_routes
from services.agent_runner.agent_host.health_routes import prepare_harness_switch_route

# meiva:note
# The outgoing history records a switch across native and external stores. For
# external-to-native transitions the writer uses the control pool with a fallback
# to the workload pool. A missing display store must not block the switch.


def _drop_agent(_agent_id: int) -> None:
    pass


def _host(**attributes: object) -> Any:
    return SimpleNamespace(drop_agent=_drop_agent, **attributes)


def _route(host: Any) -> Any:
    return prepare_harness_switch_route(
        host, clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)
    )


def _payload(agent_id: int, *, new_harness: str = "meiva") -> bytes:
    return json.dumps(
        {
            "agent_id": agent_id,
            "old_harness": "claude_code",
            "new_harness": new_harness,
            "source": "operator",
            "message": "back to the native runtime",
        }
    ).encode("utf-8")


async def _setup_agent(pool: AsyncConnectionPool[Any], agent_id: int) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (agent_id,)
        )
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code') "
            "ON CONFLICT (id) DO UPDATE SET "
            "  status = EXCLUDED.status, "
            "  harness = EXCLUDED.harness",
            (agent_id,),
        )


async def _seed_known_row(pool: AsyncConnectionPool[Any], agent_id: int) -> None:
    async with async_write_transaction(pool) as conn:
        await aappend_harness_timeline_items(
            conn,
            agent_id,
            "claude_code",
            [
                TimelineItem(
                    item_id="0.0",
                    kind="system_marker",
                    source="lifecycle_restart",
                    payload="known row",
                    created_at="2026-11-01T00:00:00+00:00",
                )
            ],
        )


async def _harness_rows(
    pool: AsyncConnectionPool[Any], agent_id: int
) -> list[tuple[str, str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT harness, item->>'kind', item->>'payload' FROM agent_harness_messages "
            "WHERE agent_id = %s ORDER BY seq ASC",
            (agent_id,),
        )
        rows = await cur.fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def _assert_switch_note(rows: list[tuple[str, str, str]]) -> None:
    assert len(rows) == 1
    harness, kind, payload = rows[0]
    assert harness == "claude_code"
    assert kind == "system_marker"
    assert "Harness switched from 'claude_code' to 'meiva'" in payload


async def test_control_pool_records_the_switch_note(
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent_id = 9211
    await _setup_agent(aops_pool, agent_id)
    host = _host(_control_pool=aops_pool)

    status_code, body, _content_type = await _route(host)(_payload(agent_id))

    assert status_code == 200
    assert b'"ok":true' in body
    _assert_switch_note(await _harness_rows(aops_pool, agent_id))


async def test_pool_fallback_records_the_switch_note(
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent_id = 9212
    await _setup_agent(aops_pool, agent_id)
    host = _host(_control_pool=None, _pool=aops_pool)

    status_code, body, _content_type = await _route(host)(_payload(agent_id))

    assert status_code == 200
    assert b'"ok":true' in body
    _assert_switch_note(await _harness_rows(aops_pool, agent_id))


async def test_switch_between_external_harnesses_leaves_the_note_to_the_destination(
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent_id = 9214
    await _setup_agent(aops_pool, agent_id)
    host = _host(_control_pool=aops_pool)

    status_code, _, _ = await _route(host)(_payload(agent_id, new_harness="codex"))

    assert status_code == 200
    assert await _harness_rows(aops_pool, agent_id) == []


async def test_no_pool_skips_the_write_and_still_answers_ok(
    aops_pool: AsyncConnectionPool[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent_id = 9213
    await _setup_agent(aops_pool, agent_id)

    # meiva:note
    # Positive control: seed one known row through the writer this branch would use, so the
    # "no new row" assertion below observes the route's behaviour rather than a query that
    # cannot see rows at all.
    await _seed_known_row(aops_pool, agent_id)
    assert await _harness_rows(aops_pool, agent_id) == [
        ("claude_code", "system_marker", "known row")
    ]

    host = _host(_control_pool=None, _pool=None)

    with caplog.at_level(logging.WARNING, logger=health_routes.__name__):
        status_code, body, _content_type = await _route(host)(_payload(agent_id))

    assert status_code == 200
    assert b'"ok":true' in body
    assert await _harness_rows(aops_pool, agent_id) == [
        ("claude_code", "system_marker", "known row")
    ]
    # meiva:note
    # The guard skips the write before attempting one, so this path stays quiet. A warning
    # here means the branch reached the writer with no pool and survived only because the
    # write is wrapped in try/except — which is not the silent skip this case claims.
    assert [record for record in caplog.records if record.name == health_routes.__name__] == []
