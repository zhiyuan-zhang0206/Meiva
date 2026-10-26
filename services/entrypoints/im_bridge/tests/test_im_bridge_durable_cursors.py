"""The push watermark survives a daemon restart, and what the agent said while
the bridge was down is pushed from the saved position.

The SSE feed is a live tail: an agent reply published while no subscription
listened is never re-delivered by it. Before the watermark was durable, a
restarted bridge had no position, so the replies of the downtime were either
never pushed (no later snapshot) or pushed together with already-delivered
history (a later snapshot carrying the whole window).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from psycopg_pool import ConnectionPool

from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.tests.slices import im_bridge_config, seeded_rng, system_clock
from services.entrypoints.im_bridge.tests.support import DurableAdapter, owned_tasks
from services.entrypoints.im_bridge.types import ChatState, InboundMessage

_KEY = ("telegram", "12345")


def _item(item_id: str, text: str, kind: str = "agent_chat") -> dict[str, Any]:
    return {
        "kind": kind,
        "item_id": item_id,
        "payload": text,
        "source_message_id": "stored-" + item_id,
        "source_block_idx": 0,
    }


class _Gateway:
    def __init__(self, timeline: list[dict[str, Any]] | None = None) -> None:
        self.timeline = timeline or []
        self.timeline_calls = 0
        self.sent: list[tuple[int, str, str | None]] = []

    async def get_timeline(self, agent_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        del agent_id, limit
        self.timeline_calls += 1
        return self.timeline

    async def send_message(
        self, agent_id: int, text: str, *, idempotency_key: str | None = None
    ) -> None:
        self.sent.append((agent_id, text, idempotency_key))

    async def stream_events(self, agent_id: int) -> Any:
        del agent_id
        await asyncio.Event().wait()
        yield None  # pragma: no cover - unreachable


class _Adapter(DurableAdapter):
    channel = "telegram"

    def __init__(self) -> None:
        super().__init__(core=None)
        self.sent: list[str] = []

    async def start(self, tasks: asyncio.TaskGroup) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del chat_id, buttons, markdown
        self.sent.append(text)


def _core(
    gateway: _Gateway,
    pool: ConnectionPool,
    state_dir: Path,
    *,
    tasks: asyncio.TaskGroup | None = None,
) -> tuple[IMBridgeCore, _Adapter]:
    core = IMBridgeCore(
        im_bridge_config(),
        cast(GatewayClient, gateway),
        db_pool=pool,
        tasks=tasks if tasks is not None else asyncio.TaskGroup(),
        state_dir=state_dir,
        clock=system_clock(),
        new_id=uuid4,
        rng=seeded_rng(),
    )
    adapter = _Adapter()
    core.register(adapter)
    return core, adapter


def _bound_state(core: IMBridgeCore) -> ChatState:
    state = core._get_or_create_state(*_KEY)
    state.current_agent_id = 405
    return state


async def test_watermark_survives_restart_so_a_snapshot_pushes_only_what_is_new(
    pool: ConnectionPool, state_dir: Path
) -> None:
    async def scenario() -> None:
        async with owned_tasks() as _owned_tasks:
            core, adapter = _core(
                _Gateway([_item("1.0", "old")]), pool, state_dir, tasks=_owned_tasks
            )
            state = _bound_state(core)
            await core._push_snapshot(_KEY, state, {"items": []})
            await core.outbound_worker.run_once()
            assert adapter.sent == ["[Meiva #405] old"]

            core2, adapter2 = _core(
                _Gateway([_item("1.0", "old"), _item("2.0", "new")]),
                pool,
                state_dir,
                tasks=_owned_tasks,
            )  # daemon restart
            await core2.restore_subscriptions()
            state2 = _bound_state(core2)
            await core2._push_snapshot(
                _KEY, state2, {"items": [_item("1.0", "old"), _item("2.0", "new")]}
            )
            await core2.outbound_worker.run_once()
            assert adapter2.sent == ["[Meiva #405] new"]

    await scenario()


async def test_restore_pushes_replies_that_arrived_while_the_bridge_was_down(
    pool: ConnectionPool, state_dir: Path
) -> None:
    async def scenario() -> None:
        async with owned_tasks() as _owned_tasks:
            core, _ = _core(_Gateway([_item("1.0", "seen")]), pool, state_dir, tasks=_owned_tasks)
            state = _bound_state(core)
            core._persist_switch(state)
            await core._push_snapshot(_KEY, state, {"items": []})
            await core.outbound_worker.run_once()

            # the bridge is down; the agent says two more things (timeline only)
            gateway2 = _Gateway(
                [_item("1.0", "seen"), _item("2.0", "missed one"), _item("3.0", "missed two")]
            )
            core2, adapter2 = _core(gateway2, pool, state_dir, tasks=_owned_tasks)
            await core2.restore_subscriptions()
            await asyncio.sleep(0.05)
            await core2.outbound_worker.run_once()
            await core2.outbound_worker.run_once()

            assert adapter2.sent == ["[Meiva #405] missed one", "[Meiva #405] missed two"]
            for task in core2._subscriptions.values():
                task.cancel()

    await scenario()


async def test_restore_without_a_saved_watermark_pushes_nothing(
    pool: ConnectionPool, state_dir: Path
) -> None:
    """A chat that was never pushed to has no position to resume from: the
    catch-up must not dump the timeline window on it."""

    async def scenario() -> None:
        async with owned_tasks() as _owned_tasks:
            core, _ = _core(_Gateway(), pool, state_dir, tasks=_owned_tasks)
            state = _bound_state(core)
            core._persist_switch(state)

            from base.db.transaction import write_transaction

            with write_transaction(pool) as conn:
                conn.execute("INSERT INTO im_bridge_cursors(channel,chat_id) VALUES (%s,%s)", _KEY)
            gateway2 = _Gateway([_item("1.0", "history")])
            core2, adapter2 = _core(gateway2, pool, state_dir, tasks=_owned_tasks)
            await core2.restore_subscriptions()
            await asyncio.sleep(0.05)

            assert adapter2.sent == []
            assert gateway2.timeline_calls > 0
            assert core2.outbound_store.pending_streams({"telegram": "test-account"}) == []
            for task in core2._subscriptions.values():
                task.cancel()

    await scenario()


async def test_idempotency_key_reaches_the_gateway(pool: ConnectionPool, state_dir: Path) -> None:
    """The adapter's platform-stable key is the gateway delivery key, so a
    message the bridge re-reads after a restart cannot become a second inbound."""

    async def scenario() -> None:
        async with owned_tasks() as _owned_tasks:
            gateway = _Gateway()
            core, _ = _core(gateway, pool, state_dir, tasks=_owned_tasks)
            _bound_state(core)
            await core.handle_inbound(
                InboundMessage(
                    channel="telegram",
                    chat_id="12345",
                    text="hello",
                    message_id="m1",
                    idempotency_key="telegram:m1",
                )
            )
            for task in core._subscriptions.values():
                task.cancel()
            assert gateway.sent == [(405, "hello", "telegram:m1")]

    await scenario()
