"""Native timeline preparation and account-frozen selection transaction admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from services.entrypoints.im_bridge.outbound.claim_fence import ClaimFence
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.outbound.types import (
    OutboundIntent,
    PushWatermark,
    TimelineAcceptance,
    TimelineCandidate,
    timeline_source,
)
from services.entrypoints.im_bridge.types import ChatState, IMAdapter, NoticeBridgeOwner, Reply


@dataclass(frozen=True)
class SelectionAdmission:
    adapter: IMAdapter
    account_id: str
    fence: ClaimFence


class TimelineCore:
    adapters: dict[str, IMAdapter]
    chats: dict[tuple[str, str], ChatState]
    outbound_store: IMOutboxStore
    notice_bridge: NoticeBridgeOwner
    _last_pushed: dict[tuple[str, str, int], PushWatermark]
    _subscriptions: dict[tuple[str, str], asyncio.Task[Any]]

    def _hold_selection(self, state: ChatState) -> None:
        raise NotImplementedError

    def _apply_selection(self, state: ChatState, selected: int | None) -> None:
        raise NotImplementedError

    def _stop_typing(self, state: ChatState) -> None:
        raise NotImplementedError

    def _get_or_create_state(self, channel: str, chat_id: str) -> ChatState:
        raise NotImplementedError

    async def _send(
        self, channel: str, chat_id: str, reply: Reply, *, adapter: IMAdapter | None = None
    ) -> None:
        raise NotImplementedError

    async def execute_spawn(
        self, state: ChatState, *, idempotency_key: str | None = None
    ) -> tuple[Reply, int | None]:
        raise NotImplementedError

    async def _handle_spawn_menu(
        self, state: ChatState, text: str, *, idempotency_key: str | None = None
    ) -> Reply | list[Reply]:
        raise NotImplementedError

    async def _handle_command(
        self,
        state: ChatState,
        text: str,
        idempotency_key: str | None = None,
        *,
        replay_id: str | None = None,
        selection_admission: SelectionAdmission | None = None,
    ) -> Reply | list[Reply] | None:
        raise NotImplementedError


async def accept_timeline(
    core: TimelineCore | object,
    state: ChatState,
    agent_id: int,
    items: list[dict[str, Any]],
    *,
    replay_id: str = "",
    switch_arg: str = "",
    selection_admission: SelectionAdmission | None = None,
) -> TimelineAcceptance:
    core_any: Any = core
    core_ref: TimelineCore = core_any
    adapter = (
        selection_admission.adapter
        if selection_admission is not None
        else core_ref.adapters[state.channel]
    )
    if selection_admission is not None and core_ref.adapters.get(state.channel) is not adapter:
        raise ValueError("selection source adapter account is superseded")
    account = await adapter.outbound_account_id()
    if selection_admission is not None and account != selection_admission.account_id:
        raise ValueError("selection source adapter account changed")
    candidates: list[TimelineCandidate] = []
    for item in items:
        source = timeline_source(item)
        intent = None
        if source is not None:
            prepared = await adapter.prepare_timeline(render_item(item, agent_id))
            intent = OutboundIntent(
                channel=state.channel,
                chat_id=state.chat_id,
                agent_id=agent_id,
                source=source,
                prepared=prepared,
                replay_id=replay_id,
            )
        candidates.append(TimelineCandidate(item, intent))
    acceptance = await asyncio.to_thread(
        core_ref.outbound_store.accept,
        state.channel,
        account,
        state.chat_id,
        agent_id,
        candidates,
        replay_id=replay_id,
        switch_arg=switch_arg,
        fence=selection_admission.fence if selection_admission is not None else None,
    )
    if acceptance.watermark is not None and acceptance.selected_agent_id == agent_id:
        core_ref._last_pushed[(state.channel, state.chat_id, agent_id)] = acceptance.watermark
    return acceptance


def render_item(it: dict[str, Any], agent_id: int | None = None) -> str:
    """One pushed line: the human's own words or the agent's text output,
    tagged so the reader always knows who is speaking (the user's format:
    ``[User]`` / ``[Meiva #<id>]``)."""

    payload = (it.get("payload") or "").strip()
    kind = it.get("kind", "")
    if kind == "inbound_chat":
        return f"[User] {payload}"
    if kind == "agent_chat":
        who = f"Meiva #{agent_id}" if agent_id is not None else "Meiva"
        return f"[{who}] {payload}"
    return f"[{kind}] {payload}"


# meiva:note
# Rebuild account-bound runtime selection.
#
# Account-bound adapters cannot import a peer-only legacy selection.
async def restore_selection(core: TimelineCore | object, state: ChatState) -> int | None:
    core_any: Any = core
    core_ref: TimelineCore = core_any
    adapter = core_ref.adapters[state.channel]
    account = await adapter.outbound_account_id()
    if adapter.selection_requires_account_proof:
        selected = await asyncio.to_thread(
            core_ref.outbound_store.native_selection, state.channel, account, state.chat_id
        )
        if core_ref.adapters.get(state.channel) is not adapter or selected is None:
            core_ref._hold_selection(state)
            return None
    else:
        selected = await asyncio.to_thread(
            core_ref.outbound_store.selection,
            state.channel,
            account,
            state.chat_id,
            state.current_agent_id,
        )
    core_ref._apply_selection(state, selected)
    return selected


def hold_selection(core: TimelineCore | object, state: ChatState) -> None:
    """Discard derived runtime state without deleting historical selection or journals."""
    core_any: Any = core
    core_ref: TimelineCore = core_any
    state.current_agent_id = None
    task = core_ref._subscriptions.pop((state.channel, state.chat_id), None)
    if task is not None:
        task.cancel()
    core_ref._stop_typing(state)


def register_adapter(core: TimelineCore | object, adapter: IMAdapter) -> None:
    core_any: Any = core
    core_ref: TimelineCore = core_any
    previous = core_ref.adapters.get(adapter.channel)
    if (
        previous is not None
        and previous is not adapter
        and adapter.selection_requires_account_proof
    ):
        for (channel, _chat), state in core_ref.chats.items():
            if channel == adapter.channel:
                hold_selection(core_ref, state)
    core_ref.adapters[adapter.channel] = adapter


def truncate(text: str, limit: int) -> str:
    """Clip to the character limit, with ASCII ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def selection_account(
    core: TimelineCore | object, state: ChatState, scope: SelectionAdmission | None
) -> str:
    core_any: Any = core
    core_ref: TimelineCore = core_any
    return (
        scope.account_id
        if scope is not None
        else await core_ref.adapters[state.channel].outbound_account_id()
    )


def apply_account_selection(
    core: TimelineCore | object,
    state: ChatState,
    agent_id: int | None,
    scope: SelectionAdmission | None,
) -> None:
    core_any: Any = core
    core_ref: TimelineCore = core_any
    if scope is None or core_ref.adapters.get(state.channel) is scope.adapter:
        core_ref._apply_selection(state, agent_id)
