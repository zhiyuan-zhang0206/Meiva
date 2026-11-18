from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from agent.graph.exec.tests.process_inputs import live_parent_facts
from base.agents.inbox.insert import insert_inbound_message
from base.agents.observation.snapshot import select_one
from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.harness import (
    BridgeEvent,
    HarnessBridge,
    HarnessInput,
    InputInContext,
    Reconciliation,
    SessionFailed,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.lm.harness.registration import SecureRandom
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir
from ops.agents.wake import transition_terminated_to_unclaimed_idling
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.runtime import AgentRuntime
from services.agent_runner.agent_host.scheduling.wake_screening import read_stored_config
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Rig, Row


class _ScriptedHarnessSession:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        exit_code: int | None = None,
        on_deliver: Any = None,
    ) -> None:
        self.session_id: str | None = "sess-1"
        self.generation = 1
        self.exit_code = exit_code
        self._start_error = start_error
        self._on_deliver = on_deliver
        self._queue: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
        self.closed = False

    async def start(self, *, resume: str | None = None) -> None:
        del resume
        if self._start_error is not None:
            raise self._start_error

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        if self._on_deliver is not None:
            self._on_deliver(inputs, self._queue)

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        return dict.fromkeys(keys, Reconciliation.UNKNOWN)

    def adopt(self, keys: Sequence[str]) -> None:
        del keys

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def interrupt(self, *, turn_id: str | None = None) -> object:
        del turn_id
        return None

    async def close(self, *, reason: str = "closed") -> None:
        del reason
        self.closed = True
        await self._queue.put(None)


def _bridge_for(session: _ScriptedHarnessSession, agent_id: int) -> HarnessBridge:
    return HarnessBridge(
        Harness.CLAUDE_CODE,
        agent_id,
        lambda _handle: session,
        timestamp_prefix=lambda: "",
        machine="this-box",
        registration_ttl_seconds=60.0,
        clock=system_clock(),
        handle_rng=SecureRandom(seeded_rng(1)),
        controller_pid=100,
    )


async def _pin_cached_bridge(
    host: Any, pool: AsyncConnectionPool[Any], agent_id: int, bridge: HarnessBridge
) -> None:
    stored = await read_stored_config(pool, agent_id)
    assert stored is not None
    host._runtimes[agent_id] = AgentRuntime(
        fingerprint=stored.fingerprint,
        llm=None,
        harness=Harness.CLAUDE_CODE,
        harness_bridge=bridge,
    )


async def test_startup_failure_persists_sanitized_reason_and_clears_stale_exit(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 701
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(
        checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=seeded_rng(0)
    )
    host = rig.host(
        {agent_id: Row(status="idling", harness="claude_code")},
        graph,
        catalog=None,
        policy=configured_policy(),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    # meiva:note
    # Seed a stale exit_code=0 from an earlier session to verify startup crash does not leave it.
    db_conn.execute(
        "UPDATE agents_meta SET harness_exit_code = 0, harness_exit_at = now() WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    proxy_val = "fixture-proxypass"
    bearer_val = "fixture-bearer-999"
    session = _ScriptedHarnessSession(
        start_error=OSError(
            "proxy connect failed "
            + "https://"
            + f"u:{proxy_val}@127.0.0.1:7890 "
            + "Authorization: Bearer "
            + bearer_val
        ),
        exit_code=None,
    )
    await _pin_cached_bridge(host, aops_pool, agent_id, _bridge_for(session, agent_id))
    insert_inbound_message(
        db_conn, agent_id, "start worker", "agent:1", bus=event_bus, database=database
    )

    try:
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '20 minutes' "
            "WHERE id = %s",
            (agent_id,),
        )
        db_conn.commit()
        await host.reap_corpses()
    finally:
        await host.aclose()

    snap = select_one(db_conn, agent_id, catalog=None, default_model="deepseek-v4-flash")
    assert snap is not None
    assert (snap.status.value, snap.harness_exit_code) == ("terminated", None)
    assert snap.last_turn_fatal_at is not None
    reason = snap.last_turn_fatal_reason or ""
    assert proxy_val not in reason
    assert bearer_val not in reason
    assert "proxy connect failed" in reason and "u:***@127.0.0.1:7890" in reason


_FIXTURE_SK_SECRET = "sk-" + "fixture-key-0002"


def _rate_limit_deliver(
    inputs: Sequence[HarnessInput], queue: asyncio.Queue[BridgeEvent | None]
) -> None:
    del inputs
    queue.put_nowait(
        TurnCompleted(
            turn_id="t1",
            session_id="sess-1",
            text="",
            subtype="success",
            is_error=True,
            interrupted=False,
            stop_reason="stop_sequence",
            num_turns=1,
            duration_ms=10,
            usage=Usage(input_tokens=1, output_tokens=1),
            cost_usd=None,
            cumulative_cost_usd=None,
            permission_denials=(),
            errors=(f"API Error: 429 quota exhausted for {_FIXTURE_SK_SECRET}",),
            error_code="rate_limit",
            generation=1,
        )
    )


def _write_claude_record(tmp_path: Path, home: Path, agent_id: int) -> None:
    workspace = workspace_dir(agent_id, home=home)
    project_dir = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "sess-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-11-19T06:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "MEIVA_DISPLAY_TO_USER\nok"}],
                },
            }
        )
        + "\n"
    )


def _succeed_deliver(tmp_path: Path, home: Path, agent_id: int) -> Any:
    def _deliver(inputs: Sequence[HarnessInput], queue: asyncio.Queue[BridgeEvent | None]) -> None:
        keys = tuple(i.key for i in inputs)
        _write_claude_record(tmp_path, home, agent_id)
        queue.put_nowait(TurnStarted("t2", TurnOrigin.INPUT, keys, 1))
        for key in keys:
            queue.put_nowait(InputInContext(key, "t2", 1))
        queue.put_nowait(
            TurnCompleted(
                turn_id="t2",
                session_id="sess-1",
                text="MEIVA_DISPLAY_TO_USER\nok",
                subtype="success",
                is_error=False,
                interrupted=False,
                stop_reason="end_turn",
                num_turns=1,
                duration_ms=10,
                usage=Usage(input_tokens=5, output_tokens=2),
                cost_usd=None,
                cumulative_cost_usd=None,
                permission_denials=(),
                generation=1,
            )
        )

    return _deliver


async def test_first_turn_stream_fault_reaps_closes_child_records_exit_and_clears_on_recovery(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 702
    base_policy = configured_policy()
    policy = replace(base_policy, environ={**base_policy.environ, "HOME": str(tmp_path)})
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(
        checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=seeded_rng(0)
    )
    host = rig.host(
        {agent_id: Row(status="idling", harness="claude_code")},
        graph,
        catalog=None,
        policy=policy,
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )
    failing_session = _ScriptedHarnessSession(exit_code=1, on_deliver=_rate_limit_deliver)
    await _pin_cached_bridge(host, aops_pool, agent_id, _bridge_for(failing_session, agent_id))
    insert_inbound_message(
        db_conn, agent_id, "first task", "agent:1", bus=event_bus, database=database
    )

    try:
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '20 minutes' "
            "WHERE id = %s",
            (agent_id,),
        )
        db_conn.commit()
        await host.reap_corpses()
        await asyncio.sleep(0.05)

        assert failing_session.closed
        snap = select_one(db_conn, agent_id, catalog=None, default_model="deepseek-v4-flash")
        assert snap is not None
        assert (snap.status.value, snap.harness_exit_code) == ("terminated", 1)
        assert snap.last_turn_fatal_reason is not None
        assert "rate_limit" in snap.last_turn_fatal_reason
        assert "quota exhausted" in snap.last_turn_fatal_reason
        assert _FIXTURE_SK_SECRET not in snap.last_turn_fatal_reason

        # meiva:note
        # Resurrect clears death diagnostics; a subsequent healthy turn stays clean.
        await host._sessions.join(agent_id)
        with db_conn.cursor() as cur:
            _ = transition_terminated_to_unclaimed_idling(
                cur, agent_id, trigger_inbound_id=None, trigger_inbound_kind=None
            )
        db_conn.commit()
        resurrected = select_one(db_conn, agent_id, catalog=None, default_model="deepseek-v4-flash")
        assert resurrected is not None
        assert (
            resurrected.last_turn_fatal_at,
            resurrected.last_turn_fatal_reason,
            resurrected.harness_exit_code,
        ) == (None, None, None)

        healthy_session = _ScriptedHarnessSession(
            exit_code=0, on_deliver=_succeed_deliver(tmp_path, host._home, agent_id)
        )
        await _pin_cached_bridge(host, aops_pool, agent_id, _bridge_for(healthy_session, agent_id))
        insert_inbound_message(
            db_conn, agent_id, "retry after fix", "user", bus=event_bus, database=database
        )
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        after_ok = select_one(db_conn, agent_id, catalog=None, default_model="deepseek-v4-flash")
        assert after_ok is not None
        assert (after_ok.last_turn_fatal_at, after_ok.last_turn_fatal_reason) == (None, None)
    finally:
        await host.aclose()


async def test_stream_crash_records_exit_code_immediately_without_waiting_for_reap(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    rig: Rig,
) -> None:
    agent_id = 703
    checkpointer = await build_checkpointer(aops_pool, checkpoint_interval=1)
    graph = build_graph(
        checkpointer, ExtensionRegistry(), parent=live_parent_facts(), rng=seeded_rng(0)
    )
    host = rig.host(
        {agent_id: Row(status="idling", harness="claude_code")},
        graph,
        catalog=None,
        policy=configured_policy(),
        checkpointer=checkpointer,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=event_bus,
    )

    def _stream_drop(
        inputs: Sequence[HarnessInput], queue: asyncio.Queue[BridgeEvent | None]
    ) -> None:
        del inputs
        queue.put_nowait(SessionFailed(error="session stream failed: EOF from proxy"))

    session = _ScriptedHarnessSession(exit_code=7, on_deliver=_stream_drop)
    await _pin_cached_bridge(host, aops_pool, agent_id, _bridge_for(session, agent_id))
    insert_inbound_message(
        db_conn, agent_id, "first task", "agent:1", bus=event_bus, database=database
    )

    try:
        await asyncio.wait_for(host.run_turn(agent_id), 15)
        await asyncio.sleep(0.05)
        snap = select_one(db_conn, agent_id, catalog=None, default_model="deepseek-v4-flash")
        assert snap is not None
        assert snap.harness_exit_code == 7
        assert snap.last_turn_fatal_at is not None
        assert snap.last_turn_fatal_reason is not None
        assert "EOF from proxy" in snap.last_turn_fatal_reason
    finally:
        await host.aclose()
