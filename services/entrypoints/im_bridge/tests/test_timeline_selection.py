import asyncio
from pathlib import Path
from typing import cast

import pytest

from services.entrypoints.im_bridge.outbound.types import OutboundIdentityConflictError
from services.entrypoints.im_bridge.tests.support import held_table_lock, owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    CoreFactory,
    FakeGateway,
    FakePlainAdapter,
    row,
)
from services.entrypoints.im_bridge.tests.test_im_bridge_core import make_core as make_core
from services.entrypoints.im_bridge.types import ChatState, PreparedOutboundSend


def gateway() -> FakeGateway:
    return FakeGateway(agents=[row(7), row(8)])


async def test_old_replay_cannot_undo_later_selection_or_depend_on_old_liveness(
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = make_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        await core._cmd_switch(state, "8", replay_id="B")
        upstream.agents = [row(8)]
        assert await core._cmd_switch(state, "7", replay_id="A") == []
        assert state.current_agent_id == 8
        assert core.outbound_store.selections() == {("telegram", "chat"): 8}
        with pytest.raises(OutboundIdentityConflictError):
            await core._cmd_switch(state, "8", replay_id="A")
        with core.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_replays").fetchone() == (
                2,
            )


async def test_accepted_switch_recovers_after_commit_before_json_cache_write(
    state_dir: Path,
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        core = make_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        blocked_cache_write = state_dir / "switch_state.json.tmp"
        blocked_cache_write.mkdir()
        with pytest.raises(IsADirectoryError):
            await core._cmd_switch(state, "7", replay_id="A")
        blocked_cache_write.rmdir()
        restarted = make_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert restarted.chats[("telegram", "chat")].current_agent_id == 7
        state2 = restarted.chats[("telegram", "chat")]
        assert await restarted._cmd_switch(state2, "7", replay_id="A") == []
        with restarted.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_replays").fetchone() == (
                1,
            )


async def test_recipient_mutex_orders_old_receipt_read_and_later_switch(
    db_url: str,
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        core = make_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        with held_table_lock(db_url, "im_bridge_outbound_replays") as held:
            old = asyncio.create_task(core._cmd_switch(state, "7", replay_id="A"))
            await asyncio.to_thread(held.wait_for_waiters, 1)
            newer = asyncio.create_task(core._cmd_switch(state, "8", replay_id="B"))
            await asyncio.sleep(0.02)
            assert not newer.done(), "B cannot apply between A's canonical read and cache apply"
        await asyncio.gather(old, newer)
        assert state.current_agent_id == 8


async def test_status_clear_is_cas_and_survives_restart(make_core: CoreFactory) -> None:
    async with owned_tasks() as _owned_tasks:
        core = make_core(gateway(), tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="A")
        cast(FakeGateway, core.gateway).agents = []
        await core._cmd_status(state)
        assert state.current_agent_id is None
        restarted = make_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert ("telegram", "chat") not in restarted._subscriptions
        assert restarted.outbound_store.selections() == {("telegram", "chat"): None}
        assert not core.outbound_store.clear_selection("telegram", "test-account", "chat", 7)


async def test_empty_switch_accepts_first_later_output_without_another_command(
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = make_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="button-event")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "first output",
                "source_message_id": "persisted-first",
                "source_block_idx": 0,
            }
        ]
        await core.poll_timeline_outbound()
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Meiva #7] first output")
        ]


@pytest.mark.parametrize("json_agent", [8, None])
async def test_legacy_json_choice_bootstraps_once_without_reviving_old_cursor(
    json_agent: int | None, state_dir: Path, make_core: CoreFactory
) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.cursor_store import PushWatermark
        from services.entrypoints.im_bridge.state import save_switch_state

        core = make_core(gateway(), tasks=_owned_tasks)
        core.cursor_store.save_push("telegram", "chat", 7, PushWatermark(None, "20.0"))
        save_switch_state(
            state_dir, {"telegram:chat": json_agent} if json_agent is not None else {}
        )
        restarted = make_core(gateway(), tasks=_owned_tasks)
        await restarted.restore_subscriptions()
        assert restarted.chats[("telegram", "chat")].current_agent_id == json_agent
        with restarted.outbound_store._pool().connection() as conn:
            assert conn.execute(
                "SELECT push_agent_id,push_item_id,push_initialized,push_account_id FROM im_bridge_cursors"
            ).fetchone() == (json_agent, None, False, "test-account")
        save_switch_state(state_dir, {"telegram:chat": 7})
        again = make_core(gateway(), tasks=_owned_tasks)
        await again.restore_subscriptions()
        assert again.chats[("telegram", "chat")].current_agent_id == json_agent


async def test_spawn_switch_button_empty_receipt_then_first_output(make_core: CoreFactory) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.types import SpawnDraft

        upstream = FakeGateway(agents=[row(777)])
        core = make_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        state.spawn_draft = SpawnDraft()
        receipt = await core._spawn_execute(state)
        assert receipt.buttons and receipt.buttons[0][1] == "/switch 777"
        await core._handle_command(state, receipt.buttons[0][1], replay_id="spawn-switch-click")
        await core._handle_chat(state, "first prompt", "prompt-event")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "first answer",
                "source_message_id": "stored-answer",
                "source_block_idx": 0,
            }
        ]
        await core.poll_timeline_outbound()
        assert upstream.sent == [(777, "first prompt", "user")]
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Meiva #777] first answer")
        ]


async def test_postcommit_acceptance_response_loss_does_not_lose_or_duplicate_send(
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = FakeGateway(
            timeline=[{"kind": "agent_chat", "item_id": "1.0", "payload": "reply"}]
        )
        core = make_core(upstream, tasks=_owned_tasks)
        state = ChatState("telegram", "chat", current_agent_id=7)
        await core._push_snapshot(("telegram", "chat"), state, {})
        restarted = make_core(upstream, tasks=_owned_tasks)
        restarted_state = ChatState("telegram", "chat", current_agent_id=7)
        await restarted._push_snapshot(("telegram", "chat"), restarted_state, {})
        await restarted.outbound_worker.run_once()
        assert cast(FakePlainAdapter, restarted.adapters["telegram"]).sent == [
            ("chat", "[Meiva #7] reply")
        ]
        assert restarted.outbound_store.pending_streams({"telegram": "test-account"}) == []


async def test_live_sse_snapshot_is_only_a_wakeup_and_periodic_pull_catches_commit(
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        upstream = gateway()
        core = make_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="empty")
        live = {
            "kind": "agent_chat",
            "item_id": "1.0",
            "payload": "not committed",
            "source_message_id": "new",
            "source_block_idx": 0,
        }
        await core._push_snapshot(("telegram", "chat"), state, {"items": [live]})
        assert core.outbound_store.pending_streams({"telegram": "test-account"}) == []
        upstream.timeline = [dict(live, payload="committed")]
        await core.poll_timeline_outbound()
        assert cast(FakePlainAdapter, core.adapters["telegram"]).sent == [
            ("chat", "[Meiva #7] committed")
        ]


class _PausingAdapter(FakePlainAdapter):
    channel = "telegram"

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare_timeline(self, text: str) -> PreparedOutboundSend:
        self.started.set()
        await self.release.wait()
        return await super().prepare_timeline(text)


async def test_live_sse_mutex_orders_acceptance_before_clear_and_late_old_producer_stays_held(
    make_core: CoreFactory,
) -> None:
    async with owned_tasks() as _owned_tasks:
        from services.entrypoints.im_bridge.tests.test_timeline_outbox import candidate

        upstream = gateway()
        core = make_core(upstream, tasks=_owned_tasks)
        state = core._get_or_create_state("telegram", "chat")
        await core._cmd_switch(state, "7", replay_id="initial")
        upstream.timeline = [
            {
                "kind": "agent_chat",
                "item_id": "1.0",
                "payload": "live",
                "source_message_id": "live-id",
                "source_block_idx": 0,
            }
        ]
        adapter = _PausingAdapter()
        core.register(adapter)
        live = asyncio.create_task(core._push_snapshot(("telegram", "chat"), state, {}))
        await adapter.started.wait()
        upstream.agents = []
        clear = asyncio.create_task(core._cmd_status(state))
        await asyncio.sleep(0.02)
        assert not clear.done(), (
            "selection clear cannot interleave a live SSE's canonical acceptance"
        )
        adapter.release.set()
        await asyncio.gather(live, clear)
        late = core.outbound_store.accept(
            "telegram", "test-account", "chat", 7, [candidate(2, account="test-account")]
        )
        assert late.blocked and not late.intent_ids and late.selected_agent_id is None
        assert core.outbound_store.selections() == {("telegram", "chat"): None}
        with core.outbound_store._pool().connection() as conn:
            assert conn.execute("SELECT count(*) FROM im_bridge_outbound_intents").fetchone() == (
                1,
            )
