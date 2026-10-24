from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from agent.graph.llm_errors import FatalLLMStreamError
from services.agent_runner.agent_host.tests.runtime.host_support import (
    AgentRows,
    Build,
    Row,
    ScriptedGraph,
)

TURN_TIMEOUT_S = 10


# meiva:note
# Records, in the database, the host-side state of an agent at the moment each of its claimed
# chats is consumed. A trigger on the inbound row writes the agent's status then, so a test
# reads from the log whether the consumption came before or after the turn's settlement.
@contextmanager
def _consumption_log(conn: psycopg.Connection) -> Generator[None]:
    conn.execute(
        "CREATE TABLE consumption_log (inbound_id bigint, agent_status text); "
        "CREATE FUNCTION log_consumption() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO consumption_log SELECT NEW.id, status FROM agents_meta "
        "WHERE id = NEW.agent_id; RETURN NEW; END $$; "
        "CREATE TRIGGER log_consumption AFTER UPDATE OF status ON inbound_messages "
        "FOR EACH ROW WHEN (OLD.status = 'claimed' AND NEW.status = 'done') "
        "EXECUTE FUNCTION log_consumption()"
    )
    conn.commit()
    try:
        yield
    finally:
        conn.execute(
            "DROP TRIGGER log_consumption ON inbound_messages; "
            "DROP FUNCTION log_consumption(); DROP TABLE consumption_log"
        )
        conn.commit()


def _consumed_while(conn: psycopg.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT agent_status FROM consumption_log")]


def _chat(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'work in flight', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _inbound(conn: psycopg.Connection, inbound_id: int) -> tuple[str, dict[str, Any] | None]:
    row = conn.execute(
        "SELECT status, payload FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


class TestSettlementReconciles:
    async def _run_claiming(
        self, wired: Build, db_conn: psycopg.Connection, result: dict[str, Any] | BaseException
    ) -> tuple[int, ScriptedGraph, AgentRows]:
        host, graph, stored = wired({1: Row(status="idling")}, {1: [result]})
        inbound = _chat(db_conn, 1)
        graph.claiming_chat.add(1)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        return inbound, graph, stored

    async def test_settled_abort_reconciles_after_the_settle(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        with _consumption_log(db_conn):
            inbound, graph, stored = await self._run_claiming(
                wired, db_conn, FatalLLMStreamError("the provider closed the stream")
            )

            assert _consumed_while(db_conn) == ["idling"], "the claim is consumed after the settle"
        assert _inbound(db_conn, inbound) == ("done", {"consumed_without_execution": True})
        assert stored.status(1) == "idling"
        assert stored.corpse_marked(1)
        assert graph.updates, "the abort was written into the checkpoint first"

    async def test_unclassified_crash_leaves_the_reconcile_to_the_next_admission(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        with pytest.raises(ValueError, match="unclassified crash"):
            await self._run_claiming(wired, db_conn, ValueError("unclassified crash"))

        claimed = db_conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = 1"
        ).fetchall()
        assert claimed == [("claimed",)], "the claim stays for the next cold admission"
        row = db_conn.execute("SELECT status FROM agents_meta WHERE id = 1").fetchone()
        assert row == ("idling",)

    async def test_finished_turn_reconciles_after_the_settle(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        with _consumption_log(db_conn):
            inbound, _, stored = await self._run_claiming(
                wired,
                db_conn,
                {"exit_requested": False, "turn_idle": True, "restart_requested": False},
            )

            assert _consumed_while(db_conn) == ["idling"], "the claim is consumed after the settle"
        assert _inbound(db_conn, inbound) == ("done", None)
        assert stored.status(1) == "idling"
        assert not stored.corpse_marked(1)
