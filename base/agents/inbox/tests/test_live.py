from __future__ import annotations

import psycopg

from base.agents.inbox.live import agent_is_alive, list_live_agent_ids
from base.db import Database
from base.db.tests.live_agents import seed_agent


def test_agent_is_alive_predicate() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    future = now + timedelta(seconds=600)
    past = now - timedelta(seconds=1)

    assert agent_is_alive("running", future) is True
    assert agent_is_alive("idling", future) is True
    assert agent_is_alive("running", None) is False
    assert agent_is_alive("running", past) is False
    assert agent_is_alive("terminated", future) is False


def test_list_live_agent_ids(db_conn: psycopg.Connection, database: Database) -> None:
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    seed_agent(db_conn, "terminated")
    assert sorted(list_live_agent_ids(database)) == sorted([running, idling])
