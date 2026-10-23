"""Claim-node test support shared by the topic files: agent status writes and waits, the hosted-owner fixture, the committed-publish filter and the compact-tail reader."""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AnyMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from psycopg_pool import AsyncConnectionPool

from agent.graph.node_log.tests.live_events import LiveEvents
from agent.state import ContextReset
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.lm.catalog import ModelCatalog
from meiva.sdk_surface import process_context

__all__ = ["committed_publishes", "compact_tail", "running_agent", "set_agent_status"]


# meiva:note
# The InboundCommitted payloads among the published live events. node_lifecycle emits a
# timeline_snapshot on every enter; only the committed-inbound behavior is wanted here.
async def committed_publishes(live: LiveEvents) -> list[dict[str, Any]]:
    return [event for event in await live.events() if event.get("role") == "inbound_committed"]


async def await_status(pool: AsyncConnectionPool, agent_id: int, expected: str) -> None:
    """Poll `agents_meta.status` on `pool` until it equals `expected`.

    The restart arm flips status through `ctx.ops_pool` (= `aops_pool`); read it
    back on the same pool the flip was written on. Raises if `expected` is not
    reached in 2s, reporting the observed status trail.

    (This helper once carried a heavy CI-flake forensic dump for an intermittent
    `idling != restarting`. That flake was a reused-id collision, killed at the
    source by the monotonic-id contract — see
    docs/decisions/engineering/tooling/2026-06-30-monotonic-test-ids.md — so
    the dump is gone; the status trail is enough for any residual failure.)
    """
    seen: list[object] = []
    row: tuple[object, ...] | None = None
    for _ in range(250):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
        current = row[0] if row is not None else None
        if not seen or seen[-1] != current:
            seen.append(current)
        if current == expected:
            return
        await asyncio.sleep(0.02)
    actual = row[0] if row is not None else None
    raise AssertionError(
        f"agent {agent_id} status {actual!r} != {expected!r} after 5s (status trail: {seen})"
    )


# meiva:note
# UPDATE `agents_meta.status` with rowcount assertion so a silent no-op UPDATE fails fast.
def set_agent_status(db: psycopg.Connection, agent_id: int, status: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        assert cur.rowcount == 1, (
            f"set_agent_status: agent {agent_id} not updated (rowcount={cur.rowcount})"
        )
    db.commit()


@pytest.fixture
async def running_agent(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    """Admit a real hosted owner and bind it throughout each dispatch test."""
    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime

    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_identity.name(),
        uuid4(),
        db=database,
    )
    assert incarnation is not None
    process_context.bind_context(
        replace(process_context.current_context(), original_incarnation=incarnation)
    )
    yield lambda: agent_id


def compact_tail(update: Mapping[str, Any] | None) -> list[AnyMessage]:
    """Assert the transport a compaction now uses — the window is cleared and
    rebuilding the standing head is handed to `init_context` — and return the
    parked tail, which is what this claim batch decided belongs behind it."""
    from langchain_core.messages.modifier import RemoveMessage as _Rm

    assert update is not None
    msgs = update["messages"]
    assert len(msgs) == 1, f"expected the sentinel alone, got {len(msgs)} messages"  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], _Rm)
    assert msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    reset = update["context_reset"]
    assert isinstance(reset, ContextReset)
    return reset.tail


# meiva:note
# Every published event for `role`, in publish order.
def _events_with_role(events: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("role") == role]


async def pair_compact_cycles(
    live: LiveEvents,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair every compact_started with its exactly-one compact_finished.

    Task #3323's completeness invariant: a started run reaches exactly one
    terminal state (success / failure / replaced), otherwise the frontend's
    ticking block would hang forever. Returns (started, finished) pairs in
    start order.
    """
    events = await live.events()
    started = _events_with_role(events, "compact_started")
    finished = _events_with_role(events, "compact_finished")
    by_id = {e["compact_id"]: e for e in finished}
    assert len(finished) == len(by_id), f"duplicate terminal for one run: {finished}"
    assert {e["compact_id"] for e in started} == set(by_id), (
        f"unpaired compact runs: started={started}, finished={finished}"
    )
    return [(s, by_id[s["compact_id"]]) for s in started]
