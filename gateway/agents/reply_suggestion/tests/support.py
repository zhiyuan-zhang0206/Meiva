# meiva:note
# A reply-suggestion harness over the real store.
#
# The agent, its status and its conversation live in the throwaway Postgres; the service reads them
# as it does in the gateway. Only the two model calls are doubles (`FakeDecide`, `FakeChat`), handed
# to the service as its required `decide` and `draft_model`.

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import psycopg
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.messages.timestamp_policy import TimestampPolicy
from base.api_contracts.agent_contract import AgentStatus
from base.clock import Clock
from base.config import settings
from base.db import Database, create_agent
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import Decision, Question
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.http.model_catalog import CatalogLoadFailure


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


def useful_reply(probability: float) -> Mapping[str, Decision]:
    return {
        "useful_reply": Decision(
            top="true" if probability >= 0.5 else "false",
            probabilities={"true": probability, "false": 1 - probability},
        )
    }


def default_policy() -> ReplySuggestionPolicy:
    return ReplySuggestionPolicy(
        enabled=True,
        model="deepseek-flash",
        gate_model="jev-1.13.0",
        gate_threshold=0.5,
        max_input_tokens=1500,
        max_candidates=3,
    )


class FakeDecide:
    def __init__(self, result: Mapping[str, Decision] | None = None) -> None:
        self.result = result if result is not None else useful_reply(0.9)
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, Mapping[str, Question], dict[str, Any]]] = []

    async def __call__(
        self, model: str, state: str, questions: Mapping[str, Question], **kwargs: Any
    ) -> Mapping[str, Decision]:
        self.calls.append((model, state, questions, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeChat:
    def __init__(
        self,
        reply: str = '{"suggestions": ["Yes, go ahead.", "Wait, explain the plan first."]}',
    ) -> None:
        self.reply = reply
        self.error: Exception | None = None
        self.built: list[str] = []
        self.invocations: list[Sequence[BaseMessage]] = []
        self.bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.usage = {"input_tokens": 120, "output_tokens": 14, "total_tokens": 134}
        self.raw = AIMessage(content=reply)
        self.metadata = {"model_provider": "anthropic", "stop_reason": "end_turn"}

    def factory(self, model: str, _catalog: ModelCatalog, _keys: ProviderKeys) -> FakeChat:
        self.built.append(model)
        return self

    def with_structured_output(self, schema: dict[str, Any], **kwargs: Any) -> FakeChat:
        self.bindings.append((schema, kwargs))
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        self.invocations.append(messages)
        if self.error is not None:
            raise self.error
        self.raw = AIMessage(
            content=self.reply, response_metadata=self.metadata, usage_metadata=self.usage
        )
        try:
            parsed = json.loads(self.reply)
        except json.JSONDecodeError as error:
            return {"raw": self.raw, "parsed": None, "parsing_error": error}
        return {"raw": self.raw, "parsed": parsed, "parsing_error": None}


# meiva:note
# One idle agent whose newest message is a question, plus a service with model doubles.
class Harness:
    def __init__(
        self, db_conn: psycopg.Connection, database: Database, catalog: ModelCatalog
    ) -> None:
        self.db_conn = db_conn
        self.database = database
        self.catalog: ModelCatalog | CatalogLoadFailure = catalog
        self.policy = default_policy()
        self.decide = FakeDecide()
        self.chat = FakeChat()
        self.service = ReplySuggestionService(decide=self.decide, draft_model=self.chat.factory)
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
            keys=None if isinstance(self.catalog, CatalogLoadFailure) else inert_keys(self.catalog),
        )
