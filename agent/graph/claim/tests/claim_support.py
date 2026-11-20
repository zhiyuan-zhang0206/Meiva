"""The claim-node test harness: a runtime around a fake LLM, a direct inbound INSERT of any kind, the thread config and the pool-visibility barrier."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Hashable, Mapping, Sequence
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.base import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

import meiva
from agent.graph.hooks.tests.scripted_model import asks_for_summary, closing_chunk
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.boundary.fields import str_field
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.agent_slices import AgentSlices
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry
from base.paths import meiva_home, resolve_meiva_home

__all__ = [
    "insert_inbound_kind",
    "make_runtime",
    "scalar",
    "scalar_int",
    "thread_config",
    "timeline_items",
    "update_of",
]


# meiva:note
# A model that answers one compaction summary, as a streamed call (the call shape of generate_summary, which binds the same tool as the main llm node). Any other generation is an assertion failure.
class SummaryModel(BaseChatModel):
    summary: str
    summary_requests: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-summary"

    def bind_tools(
        self,
        tools: Sequence[dict[str, object] | type | Callable[..., object] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: object,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise AssertionError("the claim node never makes a non-streaming request")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if not asks_for_summary(messages):
            raise AssertionError("an ordinary generation nobody scripted")
        self.summary_requests += 1
        yield ChatGenerationChunk(message=closing_chunk(AIMessage(content=self.summary)))


def make_fake_llm(summary: str = "synthetic compaction summary") -> SummaryModel:
    return SummaryModel(summary=summary)


def update_of[N: Hashable](command: Command[N]) -> dict[str, object]:
    update = object_of(getattr, command, "update")
    assert is_object_dict(update)
    return update


def snapshot_items(events: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    for event in events:
        items = event.get("items", [])
        assert is_object_list(items)
        for item in items:
            assert is_object_dict(item)
            found.append(item)
    return found


def timeline_items(events: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    return {str_field(item, "item_id"): item for item in snapshot_items(events)}


def scalar(row: tuple[object, ...] | None) -> object:
    assert row is not None
    return row[0]


def scalar_int(row: tuple[object, ...] | None) -> int:
    value = scalar(row)
    assert isinstance(value, int)
    return value


# meiva:note
# Assembles a claim Runtime borrowing the test SDK root's clients and machine. `ops_pool=None`
# takes the container early-return path. SSE fan-out goes through `ctx.event_publisher.emit`; the
# default is a sink so the node's `assert ctx.event_publisher` passes, and a test that reads the
# published events passes `live_events.publisher`.
def make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    llm: BaseChatModel | None = None,
    event_publisher: AgentEventPublisher | None = None,
    extensions: ExtensionRegistry = EMPTY,
    agent_id: int | None = None,
    timestamps_enabled: bool | None = None,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    retained = meiva.context
    ctx = MeivaContext(
        meiva_home=resolve_meiva_home(),
        environ=os.environ,
        user_home=Path.home(),
        clients=retained.clients,
        machine=retained.machine,
        original_incarnation=retained.original_incarnation,
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        ops_pool=ops_pool,
        llm=llm if llm is not None else make_fake_llm(),
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=(
                defaults()
                if timestamps_enabled is None
                else defaults(general__message_timestamps=timestamps_enabled)
            )
        ),
        clock_factory=Clock.from_settings,
        extensions=extensions,
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        catalog=build_model_catalog(home=meiva_home()),
        commands=CommandCatalog.system(skills=list, home=meiva_home()),
    )
    return Runtime(context=ctx)


def insert_inbound_kind(
    db: psycopg.Connection, tid: int, content: str, kind: str, source: str = "system"
) -> int:
    """Directly INSERT an inbound of any kind (bypasses the chat-only helper in base/db/__init__.py)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tid, content, kind, source),
        )
        new_id = scalar_int(cur.fetchone())
    db.commit()
    return new_id


async def await_inbound_visible(pool: AsyncConnectionPool, inbound_id: int) -> None:
    """Block until a `db_conn`-committed inbound row is visible on `pool`.

    Setup writes go through the sync `db_conn`; `claim_node` claims the batch
    through `aops_pool` (a different connection). Under `-n auto` there is a
    cross-connection window where a just-committed row is not yet visible on the
    pool, so a single `claim_node` call can read a partial batch and skip the
    lifecycle flip — surfacing later as a baffling `assert 'idling' ==
    'restarting'`. Prod never hits this: claim is Redis-pub/sub-driven and re-claims
    on the next wake, so the still-pending row is picked up. This barrier mirrors
    that guarantee for the test's one-shot call. Waiting on the LAST-committed
    setup row suffices: `db_conn` commits sequentially, so its visibility implies
    every earlier setup write (status, prior inbounds) is visible too.
    """
    for _ in range(100):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM inbound_messages WHERE id = %s", (inbound_id,))
            if await cur.fetchone() is not None:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"inbound {inbound_id} not visible on the claim pool after 2s")


def thread_config(tid: int) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(
                tid,
            )
        }
    }
