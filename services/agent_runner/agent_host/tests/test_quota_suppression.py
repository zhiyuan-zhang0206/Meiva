# meiva:note
# An external harness out of quota stops and waits: its turn's inputs go back to the queue, the
# recovery breaker halts it, nothing automatic runs it again for those inputs, and a new message
# resumes it with them. No reset time is read and no wake window is written.
# meiva:link base/agents/recovery/breaker.py

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from agent.graph.llm_errors import HarnessTurnHaltedError
from base.agents.messages.harness_input_key import HANDBACK_COUNT_FIELD
from base.agents.recovery.breaker import HALT_REASON_QUOTA_EXHAUSTED
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.packages.tests.model_catalog import model_catalog as model_catalog
from services.agent_runner.agent_host.tests.runtime.host_support import (
    BuildWith,
    EventTap,
    Row,
    ScriptedGraph,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    event_tap as event_tap,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_pools as host_pools,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    rig as rig,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired_with as wired_with,
)


# meiva:note
# A graph whose first turn ends the way `settle_halted_turn` ends one — the input back in the queue
# with its attempt counted, the agent halted in the same transaction — and whose later turns succeed.
class _QuotaThenWorkGraph(ScriptedGraph):
    def __init__(self, db: psycopg.Connection, agent_id: int) -> None:
        super().__init__({})
        self._db = db
        self._agent_id = agent_id
        self.turns = 0

    async def ainvoke(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.turns += 1
        if self.turns > 1:
            return {"exit_requested": False, "turn_idle": True, "restart_requested": False}
        self._db.execute(
            "UPDATE inbound_messages SET status = 'pending', claimed_at = NULL, "
            "payload = jsonb_build_object(%s::text, 1) WHERE agent_id = %s",
            (HANDBACK_COUNT_FIELD, self._agent_id),
        )
        self._db.execute(
            "UPDATE agents_meta SET permanent_reject_streak = permanent_reject_streak + 1, "
            "last_permanent_reject_reason = %s WHERE id = %s",
            (HALT_REASON_QUOTA_EXHAUSTED, self._agent_id),
        )
        self._db.commit()
        raise HarnessTurnHaltedError(
            "claude_code harness quota exhausted: You've hit your limit · resets Nov 23",
            reason=HALT_REASON_QUOTA_EXHAUSTED,
            requeued_inbound_ids=(),
        )


def _chat(db: psycopg.Connection, agent_id: int, content: str, source: str) -> None:
    db.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (%s, %s, 'chat', %s, 'pending')",
        (agent_id, content, source),
    )
    db.commit()


# meiva:note
# Stopped (idling, not a crash corpse for the reaper; halted; no wake window; input kept) and
# alerted (the turn's Error event names the quota); the requeued input alone never runs the agent
# again however often a wake comes; a message written after the stop resumes it and lifts the halt.
async def test_quota_abort_stops_and_alerts_then_a_new_message_resumes_the_agent(
    wired_with: BuildWith,
    db_conn: psycopg.Connection,
    event_tap: EventTap,
) -> None:
    agent_id = 17
    graph = _QuotaThenWorkGraph(db_conn, agent_id)
    host, _, _ = wired_with({agent_id: Row(status="idling", harness="claude_code")}, graph)
    _chat(db_conn, agent_id, "do the work", "user")

    await asyncio.wait_for(host.run_turn(agent_id), 10)

    row = db_conn.execute(
        "SELECT status, last_turn_fatal_at, permanent_reject_streak, "
        "last_permanent_reject_reason, wake_suppressed_until FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("idling", None, 1, HALT_REASON_QUOTA_EXHAUSTED, None)
    pending = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND status = 'pending'",
        (agent_id,),
    ).fetchone()
    assert pending == (1,)
    errors = await event_tap.events("error")
    assert len(errors) == 1
    assert "quota exhausted" in errors[0]["content"]

    for _ in range(3):
        await asyncio.wait_for(host.run_turn(agent_id), 10)
    assert graph.turns == 1

    _chat(db_conn, agent_id, "quota is reset, go on", "user")
    await asyncio.wait_for(host.run_turn(agent_id), 10)
    assert graph.turns == 2
    halt = db_conn.execute(
        "SELECT permanent_reject_streak, last_permanent_reject_reason FROM agents_meta "
        "WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert halt == (0, None)
