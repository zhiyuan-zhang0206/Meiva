# meiva:note
# A reply-suggestion harness over throwaway Postgres and a real loopback HTTP provider.
# The Jev binding and LangChain Responses adapter own wire parsing; tests supply response facts.

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Any

import psycopg
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.messages.timestamp_policy import TimestampSource
from base.api_contracts.agent_contract import AgentStatus
from base.clock import Clock
from base.config import settings
from base.db import Database, create_agent
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import DecisionBinding, DecisionBuildContext
from base.lm.catalog.provider_contract import BuildContext
from base.lm.catalog.provider_keys import ProviderKeys
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.agents.reply_suggestion.tests.provider_wire import LocalTransport, ProviderWire
from gateway.http.model_catalog import CatalogLoadFailure

DRAFT_PROVIDER_PREFIX = "deepseek-"


# meiva:note
# The service exercises the same Responses adapter used by DeepSeek's native structured-output
# path. Its endpoint is local; parsing, stop metadata, usage and cancellation remain LangChain's.
# Provider construction itself is covered by the provider's factory tests.
def with_provider_wire(catalog: ModelCatalog, wire: ProviderWire) -> ModelCatalog:
    from langchain_openai import ChatOpenAI

    binding = catalog.bindings[DRAFT_PROVIDER_PREFIX]

    def build(context: BuildContext) -> BaseChatModel:
        return ChatOpenAI(
            model=context.model,
            api_key=context.keys.require(binding.key_env),
            base_url=wire.url,
            use_responses_api=True,
            reasoning_effort="none",
            timeout=context.timeout,
            max_retries=0,
        )

    local = replace(binding, build=build)
    decision_provider = catalog.decision_provider_of("jev-1.13.0")
    assert decision_provider is not None

    original_build = decision_provider.build

    def build_gate(context: DecisionBuildContext) -> DecisionBinding:
        return original_build(replace(context, transport=LocalTransport(wire.url)))

    return replace(
        catalog,
        bindings=MappingProxyType({**catalog.bindings, binding.prefix: local}),
        decision_providers=MappingProxyType(
            {
                **catalog.decision_providers,
                decision_provider.prefix: replace(decision_provider, build=build_gate),
            }
        ),
    )


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
        wire: ProviderWire,
        keys: ProviderKeys,
    ) -> None:
        self.db_conn = db_conn
        self.database = database
        self.real_catalog = catalog
        self.catalog: ModelCatalog | CatalogLoadFailure = with_provider_wire(catalog, wire)
        self.keys = keys
        self.policy = default_policy()
        self.wire = wire
        self.service = ReplySuggestionService(llm_override="", transport=None)
        self.timestamps = TimestampSource(clock=Clock.from_settings(), timestamps_enabled=False)
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
            rendering=self.timestamps.policy(),
            catalog=self.catalog,
            keys=None if isinstance(self.catalog, CatalogLoadFailure) else self.keys,
        )
