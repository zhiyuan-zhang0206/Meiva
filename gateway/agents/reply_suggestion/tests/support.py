# meiva:note
# A reply-suggestion harness over the real store, the real gate wire and the real chat-model build.
#
# The agent, its status and its conversation live in the throwaway Postgres; the service reads them
# as it does in the gateway. The gate's question goes through `decide` over an HTTP transport that
# answers the provider's wire from a script (`GateServer`), and the draft is built by
# `build_chat_model` from a catalog whose drafting provider builds a `DraftChat`. What a test
# changes between requests is the agent's rows, the policy and the catalog, never a double.

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import httpx
import psycopg
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import Field

from base.agents.messages.timestamp_policy import TimestampPolicy
from base.api_contracts.agent_contract import AgentStatus
from base.clock import Clock
from base.config import settings
from base.db import Database, create_agent
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import BuildContext
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy
from gateway.agents.reply_suggestion.schemas import DraftReplies, ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.http.model_catalog import CatalogLoadFailure

DRAFT_PROVIDER_PREFIX = "deepseek-"
DEFAULT_REPLY = '{"suggestions": ["Yes, go ahead.", "Wait, explain the plan first."]}'


# meiva:note
# One answer of the gate provider: a probability for the gate's yes/no question, a ready HTTP
# response (an error status, a malformed body) or an exception the transport raises.
GateReply = float | httpx.Response | Exception


# meiva:note
# The gate provider's HTTP wire, as a transport that answers from a script.
#
# The script is given once; the n-th request gets the n-th reply and the last reply repeats.
# `requests` are the calls that reached the wire, so "the gate was not asked" is `requests == []`.
class GateServer:
    def __init__(self, *script: GateReply) -> None:
        if not script:
            raise ValueError("a gate script needs at least one reply")
        self._script = list(script)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"useful_reply": {"type": "noul", "noul": reply}},
                "usage": {"input_tokens": 80, "output_tokens": 0},
            },
        )

    # meiva:note
    # The JSON bodies of the requests that reached the wire, in order.
    def asked(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


# meiva:note
# A chat model whose structured-output call answers `reply` the way a provider's does.
#
# `reply` is parsed as JSON for the structured result; `stop` and `usage` shape the raw
# message the stop and usage accounting read. With `error` the call raises it; with `started` the
# call announces itself and then waits until it is cancelled, setting `cancelled` when it is.
class DraftChat(BaseChatModel):
    reply: str = DEFAULT_REPLY
    stop: dict[str, str] = Field(
        default_factory=lambda: {"model_provider": "anthropic", "stop_reason": "end_turn"}
    )
    usage: dict[str, int] = Field(
        default_factory=lambda: {"input_tokens": 120, "output_tokens": 14, "total_tokens": 134}
    )
    error: Exception | None = None
    started: Any = None
    cancelled: Any = None

    @property
    def _llm_type(self) -> str:
        return "draft-chat"

    def _generate(self, messages: list[BaseMessage], *_args: Any, **_kwargs: Any) -> ChatResult:
        raise NotImplementedError("the drafting service only asks for structured output")

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[Any, Any]:
        assert schema == DraftReplies.model_json_schema()
        assert (kwargs, include_raw) == ({"method": "json_schema", "strict": True}, True)
        return RunnableLambda(self._structured)

    async def _structured(self, _messages: Sequence[BaseMessage]) -> dict[str, Any]:
        if self.started is not None:
            self.started.set()
            try:
                await _forever()
            except BaseException:
                self.cancelled.set()
                raise
        if self.error is not None:
            raise self.error
        raw = AIMessage(content=self.reply, response_metadata=self.stop, usage_metadata=self.usage)
        try:
            return {"raw": raw, "parsed": json.loads(self.reply), "parsing_error": None}
        except json.JSONDecodeError as error:
            return {"raw": raw, "parsed": None, "parsing_error": error}


async def _forever() -> None:
    import asyncio

    await asyncio.sleep(30)


# meiva:note
# `catalog` with the drafting provider's chat build answering `chat`: the model rows, prices and
# stop vocabulary stay the real ones, so the usage accounting runs for real.
def with_draft_chat(catalog: ModelCatalog, chat: DraftChat) -> ModelCatalog:
    binding = catalog.bindings[DRAFT_PROVIDER_PREFIX]

    def build(_context: BuildContext) -> BaseChatModel:
        return chat

    faked = replace(binding, build=build)
    return replace(catalog, bindings={**catalog.bindings, DRAFT_PROVIDER_PREFIX: faked})


# meiva:note
# Store `messages` as the agent's checkpoint `version` (the highest version is the live head).
def put_checkpoint(agent_id: int, messages: list[BaseMessage], *, version: str) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=CheckpointMetadata(source="input", step=int(version), parents={}),
            new_versions={"messages": version},
        )


def inbound(text: str, source: str = "user") -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": source,
            "meiva_created_at": "2026-10-11T00:00:00+00:00",
        },
    )


def agent_text(text: str, message_id: str = "ai-1") -> AIMessage:
    return AIMessage(content=text, id=message_id)


def conversation(question: str = "Deploy now?") -> list[BaseMessage]:
    return [
        SystemMessage(content="system prompt"),
        inbound("Please prepare the release."),
        agent_text(question),
    ]


def default_policy() -> ReplySuggestionPolicy:
    return ReplySuggestionPolicy(
        enabled=True,
        model="deepseek-flash",
        gate_model="jev-1.13.0",
        gate_threshold=0.5,
        max_input_tokens=1500,
        max_candidates=3,
    )


# meiva:note
# One idle agent whose newest message is a question, plus a service wired to the gate wire and
# the drafting provider the test gave it.
class Harness:
    def __init__(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        catalog: ModelCatalog,
        *,
        gate: GateServer,
        chat: DraftChat,
    ) -> None:
        self.db_conn = db_conn
        self.database = database
        self.real_catalog = catalog
        self.catalog: ModelCatalog | CatalogLoadFailure = with_draft_chat(catalog, chat)
        self.keys: ProviderKeys = inert_keys(catalog)
        self.policy = default_policy()
        self.gate = gate
        self.chat = chat
        self.service = ReplySuggestionService(llm_override="", transport=gate.transport)
        self.rendering = TimestampPolicy(clock=Clock.from_settings(), timestamps_enabled=False)
        self.agent_id = self._create_idle_agent()
        self._versions = 0
        self.store(conversation())

    def _create_idle_agent(self) -> int:
        agent_id = create_agent(self.db_conn)
        self.db_conn.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', 'idling', 'm')",
            (agent_id,),
        )
        self.db_conn.commit()
        return agent_id

    def store(self, messages: list[BaseMessage], *, agent_id: int | None = None) -> None:
        self._versions += 1
        put_checkpoint(
            self.agent_id if agent_id is None else agent_id, messages, version=str(self._versions)
        )

    def new_agent(self, messages: list[BaseMessage] | None = None) -> int:
        agent_id = self._create_idle_agent()
        self.store(conversation() if messages is None else messages, agent_id=agent_id)
        return agent_id

    def set_status(self, status: AgentStatus) -> None:
        self.db_conn.execute(
            "UPDATE agents_meta SET status = %s WHERE id = %s", (status.value, self.agent_id)
        )
        self.db_conn.commit()

    def configure(self, **changes: Any) -> None:
        self.policy = replace(self.policy, **changes)

    async def suggest(self, agent_id: int | None = None) -> ReplySuggestionResponse:
        return await self.service.suggest(
            self.agent_id if agent_id is None else agent_id,
            db=self.database,
            policy=self.policy,
            rendering=self.rendering,
            catalog=self.catalog,
            keys=None if isinstance(self.catalog, CatalogLoadFailure) else self.keys,
        )
