# meiva:note
# The pieces the llm node tests share: a chat model that plays scripted tries, a stream handler that can fail, and the runtime a node runs in. The model is the node's real collaborator (`ctx.llm`); nothing here replaces a name in another module.
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal
from unittest.mock import MagicMock

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langgraph.runtime import Runtime
from pydantic import Field, SkipValidation
from redis.asyncio.client import PubSub

from agent.graph._callbacks import RedisStreamHandler
from base.agents.context import MeivaContext
from base.agents.observation.turn_progress import TurnProgress
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog

# meiva:note
# One step of a scripted try: a chunk to stream, an error to raise, seconds to sleep, "hang" to stop answering for good, or "echo" to stream the tools bound and the messages shown as one chunk.
Step = AIMessageChunk | BaseException | float | Literal["hang", "echo"]


# meiva:note
# The final chunk of a normal answer: stop reason and usage are what the node's validators require of the last chunk.
def end_turn_chunk(text: str = "ok", *, output_tokens: int = 1) -> AIMessageChunk:
    return AIMessageChunk(
        content=text,
        response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": output_tokens,
            "total_tokens": 1 + output_tokens,
        },
    )


# meiva:note
# Each streaming request plays the next script in `scripts`; what is left in `scripts` afterwards is the number of tries the caller never made. A non-streaming call is a failure of the caller, since the node has no fallback request.
class ScriptedModel(BaseChatModel):
    scripts: SkipValidation[list[Sequence[Step]]] = Field(default_factory=list)
    tools: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "scripted-llm-node-test"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedModel:
        del kwargs
        return self.model_copy(update={"tools": tuple(tool.name for tool in tools)})

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the llm node streams the model asynchronously")

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise AssertionError("the llm node made a non-streaming request")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, run_manager, kwargs
        for step in self.scripts.pop(0):
            if isinstance(step, AIMessageChunk):
                yield ChatGenerationChunk(message=step)
            elif isinstance(step, BaseException):
                raise step
            elif step == "hang":
                await asyncio.Future[None]()
            elif step == "echo":
                shown = " | ".join(str(message.content) for message in messages)
                yield ChatGenerationChunk(message=AIMessageChunk(content=f"{self.tools}: {shown}"))
            else:
                await asyncio.sleep(step)


# meiva:note
# A stream handler that takes every chunk, or raises `fail_with` on the first, as a failing live-view fan-out would.
def StubHandler(fail_with: BaseException | None = None) -> RedisStreamHandler:  # noqa: N802
    publisher = MagicMock(spec=AgentEventPublisher)
    if fail_with is not None:
        publisher.emit.side_effect = fail_with
    return RedisStreamHandler(
        event_publisher=publisher,
        agent_id=1,
        msg_idx=0,
        turn_progress=TurnProgress(),
    )


def sink_publisher() -> AgentEventPublisher:
    return MagicMock(spec=AgentEventPublisher)


def llm_runtime(
    llm: BaseChatModel,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    pins: Mapping[str, Any] | None = None,
    event_publisher: AgentEventPublisher | None = None,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            ops_pool=None,
            llm=llm,
            event_publisher=event_publisher or sink_publisher(),
            agent=AgentSlices.resolve(pins, default_reader=defaults()),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            catalog=model_catalog,
            clock_factory=Clock.from_settings,
        )
    )


# meiva:note
# The payloads a real publisher puts on the events channel of the throwaway Redis, read after the publisher has drained.
@asynccontextmanager
async def published_payloads(
    event_bus: EventBus,
) -> AsyncGenerator[tuple[AgentEventPublisher, list[str]]]:
    payloads: list[str] = []
    publisher = AgentEventPublisher(
        event_bus.async_redis(), event_bus.channel, agent_id=1, auth_retry=event_bus.auth_retry
    )
    async with (
        event_bus.open_async_redis() as client,
        PubSub(client.connection_pool) as subscription,
    ):
        await subscription.subscribe(event_bus.channel)
        await subscription.get_message(timeout=5)
        async with asyncio.TaskGroup() as workers:
            await publisher.start(workers)
            yield publisher, payloads
            await publisher.aclose()
        while (
            message := await subscription.get_message(ignore_subscribe_messages=True, timeout=0.5)
        ) is not None:
            data = message["data"]
            if isinstance(data, str):
                payloads.append(data)
