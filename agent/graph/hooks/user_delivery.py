import asyncio
from collections.abc import Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent.graph.llm_errors import FatalLLMStreamError
from agent.messages import system_note_message
from base.agents.context import MeivaContext
from base.agents.history.timeline import ai_message_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.reply_intent import SDK_DELIVERY_RULE
from base.agents.to_user.delivery.store import (
    DeliveryConflictError,
    latest_decision,
    messages,
    pending_context,
    publish,
)
from base.api_contracts.user_delivery import PublishMessage, UserDeliveryMode
from base.events.live.announce import publish_agent_updated
from base.log import logger


async def delivery_baseline(ctx: MeivaContext, agent_id: int) -> int:
    if ctx.db is None or ctx.require_agent().prompt.user_delivery_mode != "sdk":
        return 0

    def read() -> int:
        with ctx.require_db().connect() as conn:
            return latest_decision(conn, agent_id)

    return await asyncio.to_thread(read)


async def capture_items(
    ctx: MeivaContext,
    agent_id: int,
    items: list[TimelineItem],
    *,
    source_key: str,
) -> None:
    await capture_keyed_items(
        ctx, agent_id, [(f"{source_key}:{item.item_id}", item) for item in items]
    )


# meiva:note
# Publish the visible replies among `keyed` (a publication key and the item it publishes), each once: a key that
# is already published with the same content is a no-op. `tolerate_conflicts` makes a key that is already published
# with other content a warning and not an error.
async def capture_keyed_items(
    ctx: MeivaContext,
    agent_id: int,
    keyed: Sequence[tuple[str, TimelineItem]],
    *,
    tolerate_conflicts: bool = False,
) -> None:
    mode = UserDeliveryMode(ctx.require_agent().prompt.user_delivery_mode)
    if ctx.db is None or mode is UserDeliveryMode.SDK:
        return
    visible = [
        (key, item)
        for key, item in keyed
        if item.kind == "agent_chat"
        and item.user_facing
        and not item.activity_pending
        and item.payload.strip()
    ]
    if not visible:
        return
    now = ctx.require_clock().now()

    def write() -> None:
        with ctx.require_db().connect() as conn:
            for key, item in visible:
                try:
                    publish(
                        conn,
                        agent_id,
                        key,
                        PublishMessage(content=item.payload, suggestions=[]),
                        mode=mode,
                        created_at=datetime.fromisoformat(item.created_at)
                        if item.created_at
                        else now,
                    )
                except DeliveryConflictError:
                    if not tolerate_conflicts:
                        raise
                    logger.warning(
                        "a reply's publication key already names other content; it is left as it was",
                        event="harness_reply_key_conflict",
                        agent_id=agent_id,
                        source_key=key,
                    )

    await asyncio.to_thread(write)
    await publish_agent_updated(ctx.require_bus(), agent_id)


async def capture_reply(ctx: MeivaContext, agent_id: int, message: AIMessage) -> None:
    if ctx.db is None or ctx.ops_pool is None:
        return
    if message.id is None:
        raise ValueError("assistant delivery requires a stable message id")
    await capture_items(
        ctx, agent_id, ai_message_items(message, 0, {}), source_key=f"assistant:{message.id}"
    )


async def with_pending_deliveries[T: BaseMessage](
    ctx: MeivaContext,
    agent_id: int,
    history: list[T],
) -> list[T | HumanMessage]:
    if ctx.db is None or ctx.require_agent().prompt.user_delivery_mode != "sdk":
        return [*history]

    def read() -> str:
        with ctx.require_db().connect() as conn:
            return pending_context(messages(conn, agent_id, pending_only=True, limit=21))

    pending = await asyncio.to_thread(read)
    if not pending:
        return [*history]
    now = ctx.require_clock().now()
    return [
        *history,
        system_note_message(
            content="Unread publications remain durable independently of your workspace overview.\n\n"
            + pending,
            tag=NoteTag.REPLY_VISIBILITY,
            picked_up_at=now,
            created_at=now,
        ),
    ]


async def delivery_end_reminder(
    ctx: MeivaContext,
    agent_id: int,
    baseline: int | None,
    *,
    correcting: bool,
) -> HumanMessage | None:
    if (
        ctx.db is None
        or ctx.require_agent().prompt.user_delivery_mode != "sdk"
        or baseline is None
        or await delivery_baseline(ctx, agent_id) > baseline
    ):
        return None
    if correcting:
        raise FatalLLMStreamError(
            "SDK delivery contract violated: the turn ended twice without publishing, asking, or recording no update"
        )
    return publication_reminder(ctx.require_clock().now())


def publication_reminder(now: datetime) -> HumanMessage:
    return system_note_message(
        content="This turn ended without publishing, asking, or explicitly recording no update. "
        + SDK_DELIVERY_RULE,
        tag=NoteTag.REPLY_VISIBILITY,
        picked_up_at=now,
        created_at=now,
    )
