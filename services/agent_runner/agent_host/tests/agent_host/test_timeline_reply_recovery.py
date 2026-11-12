# meiva:note
# Which replies of a transcript-sourced timeline the effects hand to delivery, and which they never do again. The
# writer commits a reply and then hands it over in a step of its own, so what a restart must offer again is the
# replies the writer committed after the agent's reply progress and nothing else: not the history a fork copied, not
# what was written while delivery was `sdk` (which hands nothing over), not a reply a previous process handed over. A
# reply that conflicts with a recorded delivery is dropped with an event and holds none of the others, a failure that
# repeats is reported once, a recovery and a first commit that interleave hand every reply over once, and what is
# read is the part after the progress, so it does not grow with the history. Real Postgres; the capture is the
# real one where the delivery mode matters and a recording fake otherwise.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import asyncio
from itertools import count
from typing import cast
from unittest.mock import Mock

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.tests.test_llm_helpers import make_runtime
from agent.harness_timeline.plan import WritePlan
from agent.harness_timeline.store import ainit_reply_progress, aload_unconfirmed_replies
from agent.harness_timeline.writer import Committed
from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.history.harness_messages import aappend_keyed_timeline_items, copy_harness_timeline
from base.agents.history.timeline_item import TimelineItem
from base.agents.to_user.delivery.store import DeliveryConflictError
from base.clock.tests.fakes import system_clock
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources
from services.agent_runner.agent_host.transcript.effects import OWED_LIMIT, TimelineEffects


class Published:
    def emit(self, payload: str) -> None:
        del payload


class Delivery:
    def __init__(self) -> None:
        self.handed: list[str] = []
        self.conflicts: set[str] = set()
        self.fail = False
        self.pause = 0.0
        self.attempts = 0

    async def capture(self, ctx: object, agent_id: int, items: list[TimelineItem]) -> None:
        del ctx, agent_id
        self.attempts += 1
        if self.pause:
            await asyncio.sleep(self.pause)
        if self.fail:
            raise OSError("the delivery store is down")
        for item in items:
            if item.payload in self.conflicts:
                raise DeliveryConflictError(item.payload)
            self.handed.append(item.payload)


def _effects(pool: AsyncConnectionPool, agent_id: int, delivery: Delivery) -> TimelineEffects:
    effects = TimelineEffects(
        agent_id, Published(), pool=pool, clock=system_clock(), capture=delivery.capture
    )
    effects.bind(cast("MeivaContext", Mock()))
    return effects


_keys = count()


def _reply(text: str) -> TimelineItem:
    return TimelineItem(item_id="x", kind="agent_chat", payload=text, user_facing=True)


# meiva:note
# What the writer's commit does to the timeline (progress made, keyed rows appended), without a transcript to read;
# `after_commit` is what the writer calls next, and a test that does not call it is a process that died in between.
async def _commit(
    pool: AsyncConnectionPool, agent_id: int, *texts: str
) -> tuple[TimelineItem, ...]:
    async with pool.connection() as conn:
        await ainit_reply_progress(conn, agent_id)
        written = await aappend_keyed_timeline_items(
            conn,
            agent_id,
            "claude_code",
            [(f"k:{next(_keys)}", _reply(text)) for text in texts],
            projection_version=1,
        )
    return tuple(written)


async def _agent(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = await cursor.fetchone()
        assert row is not None
        agent_id = int(row[0])
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code')",
            (agent_id,),
        )
    return agent_id


async def _progress(pool: AsyncConnectionPool, agent_id: int) -> int | None:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT replies_seq FROM agent_harness_timeline_progress WHERE agent_id = %s",
            (agent_id,),
        )
        row = await cursor.fetchone()
    return None if row is None else int(row[0])


async def test_a_fork_offers_none_of_its_parents_replies_and_only_its_own_after(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    parent = await _agent(aops_pool)
    parent_delivery = Delivery()
    parent_effects = _effects(aops_pool, parent, parent_delivery)
    written = await _commit(aops_pool, parent, "p1", "p2")
    await parent_effects.after_commit(Committed(WritePlan(), written))
    assert parent_delivery.handed == ["p1", "p2"]

    child = await _agent(aops_pool)
    with db_conn.cursor() as cur:
        assert copy_harness_timeline(cur, parent, child) == 2
    db_conn.commit()
    delivery = Delivery()
    effects = _effects(aops_pool, child, delivery)
    await effects.recover()
    assert delivery.handed == [], "a fork has no unread: nothing it inherited is offered"

    own = await _commit(aops_pool, child, "c1")
    await effects.after_commit(Committed(WritePlan(), own))
    assert delivery.handed == ["c1"], "only what the fork's own writer committed is handed over"

    restarted = Delivery()
    await _effects(aops_pool, child, restarted).recover()
    assert restarted.handed == [], "and it is not offered again"


async def test_what_was_written_while_delivery_was_sdk_is_not_back_filled_in_another_mode(
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = await _agent(aops_pool)

    def ctx(mode: str) -> MeivaContext:
        return make_runtime(
            resources=hosted_resources,
            database_gate=database_gate,
            pins={"user_delivery_mode": mode},
        ).context

    effects = TimelineEffects(agent_id, Published(), pool=aops_pool, clock=system_clock())
    effects.bind(ctx("sdk"))
    first = await _commit(aops_pool, agent_id, "written under sdk")
    await effects.after_commit(Committed(WritePlan(), first))
    assert await _progress(aops_pool, agent_id) == 0, (
        "the reply was through the step although sdk hands nothing over"
    )

    effects.bind(ctx("naive"))
    await effects.recover()
    assert db_conn.execute(
        "SELECT count(*) FROM agent_publications WHERE agent_id = %s", (agent_id,)
    ).fetchone() == (0,), "switching the mode does not deliver what was written before"
    later = await _commit(aops_pool, agent_id, "written under naive")
    await effects.after_commit(Committed(WritePlan(), later))
    assert db_conn.execute(
        "SELECT content FROM agent_publications WHERE agent_id = %s", (agent_id,)
    ).fetchall() == [("written under naive",)]


async def test_a_crash_between_the_commit_and_the_effects_costs_only_that_batch_exactly_once(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    effects = _effects(aops_pool, agent_id, delivery)
    done = await _commit(aops_pool, agent_id, "a", "b")
    await effects.after_commit(Committed(WritePlan(), done))
    _ = await _commit(aops_pool, agent_id, "c", "d")
    assert delivery.handed == ["a", "b"]

    restarted = Delivery()
    await _effects(aops_pool, agent_id, restarted).recover()
    assert restarted.handed == ["c", "d"], "only the batch whose effects did not run"
    again = Delivery()
    await _effects(aops_pool, agent_id, again).recover()
    assert again.handed == [], "and once"


async def test_a_reply_that_conflicts_is_dropped_with_an_event_and_holds_none_of_the_others(
    aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    delivery.conflicts = {"bad"}
    effects = _effects(aops_pool, agent_id, delivery)
    written = await _commit(aops_pool, agent_id, "one", "bad", "three")
    await effects.after_commit(Committed(WritePlan(), written))
    assert delivery.handed == ["one", "three"]
    assert [
        r["extra"]["item_id"] for r in loguru_records.events("harness_timeline_reply_dropped")
    ] == [written[1].item_id]
    assert not loguru_records.events("harness_timeline_effect_failed")
    later = Delivery()
    await _effects(aops_pool, agent_id, later).recover()
    assert later.handed == [], "the dropped reply is not offered again"


async def test_a_failing_delivery_holds_the_replies_costs_no_other_effect_and_is_reported_once(
    aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    delivery.fail = True
    effects = _effects(aops_pool, agent_id, delivery)
    for text in ("a", "b", "c"):
        written = await _commit(aops_pool, agent_id, text)
        await effects.after_commit(Committed(WritePlan(), written))
    assert delivery.handed == []
    failures = loguru_records.events("harness_timeline_effect_failed")
    assert [r["extra"]["effect"] for r in failures] == ["capture"], "three failures, one report"
    delivery.fail = False
    await effects.recover()
    assert delivery.handed == ["a", "b", "c"], "held in order, handed over once the store is back"


async def test_a_recover_and_a_first_commit_that_interleave_hand_every_reply_over_once(
    aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    delivery.pause = 0.01
    effects = _effects(aops_pool, agent_id, delivery)
    first = await _commit(aops_pool, agent_id, "a", "b")
    _ = await _commit(aops_pool, agent_id, "c")
    await asyncio.gather(
        effects.recover(),
        effects.after_commit(Committed(WritePlan(), first)),
        effects.after_commit(Committed(WritePlan(), ())),
    )
    assert delivery.handed == ["a", "b", "c"]
    assert not loguru_records.events("harness_timeline_effect_failed")


async def test_the_progress_follows_the_batches_and_a_pass_reads_only_what_is_after_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    effects = _effects(aops_pool, agent_id, delivery)
    seen: list[int] = []
    for batch in range(12):
        written = await _commit(aops_pool, agent_id, f"{batch}.a", f"{batch}.b")
        async with aops_pool.connection() as conn:
            owed = await aload_unconfirmed_replies(conn, agent_id, limit=OWED_LIMIT)
        assert owed is not None
        seen.append(len(owed.replies))
        await effects.after_commit(Committed(WritePlan(), written))
        assert await _progress(aops_pool, agent_id) == 2 * batch + 1
    assert seen == [2] * 12, (
        "every pass saw the one batch after the progress, however long the history"
    )
    assert len(delivery.handed) == 24 and delivery.attempts == 24, "each reply was offered once"


async def test_a_backlog_longer_than_one_read_is_handed_over_in_pages(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = await _agent(aops_pool)
    delivery = Delivery()
    effects = _effects(aops_pool, agent_id, delivery)
    _ = await _commit(aops_pool, agent_id, "seed")
    await effects.recover()
    texts = [str(index) for index in range(OWED_LIMIT + 5)]
    _ = await _commit(aops_pool, agent_id, *texts)
    await effects.recover()
    assert delivery.handed == ["seed", *texts]
    assert await _progress(aops_pool, agent_id) == OWED_LIMIT + 5
