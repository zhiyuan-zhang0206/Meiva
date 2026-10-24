# meiva:note
# The suggestion service against a real store: it reads the agent, gates, drafts and remembers.
#
# The judgments are asserted in `test_plan.py`; here the agent, its status and its conversation are
# rows in the throwaway Postgres, the gate's question travels over the provider's wire (a transport
# that answers from a script) and the draft is the chat model the catalog builds. What a test
# varies is the script, the reply or the stored rows, so each outcome is read from the result, the
# log and the wire.

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from langchain_core.messages import SystemMessage

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from gateway.agents.reply_suggestion.service import (
    DRAFT_USAGE_KIND,
    GATE_USAGE_KIND,
    ReplySuggestionConfigurationError,
)
from gateway.agents.reply_suggestion.tests.support import (
    DraftChat,
    GateServer,
    Harness,
    agent_text,
    conversation,
    inbound,
)
from gateway.http.model_catalog import CatalogLoadFailure, ModelCatalogUnavailableError

HarnessFactory = Callable[..., Harness]
_PROVIDER_DOWN = httpx.Response(500, json={"error": "provider down"})
_TWO_REPLIES = ["Yes, go ahead.", "Wait, explain the plan first."]


@pytest.fixture
def make_harness(
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog
) -> HarnessFactory:
    def make(*, gate: GateServer | None = None, chat: DraftChat | None = None) -> Harness:
        return Harness(
            db_conn,
            database,
            model_catalog,
            gate=GateServer(0.9) if gate is None else gate,
            chat=DraftChat() if chat is None else chat,
        )

    return make


@pytest.fixture
def harness(make_harness: HarnessFactory) -> Harness:
    return make_harness()


def records_named(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == event]


def usage_of(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [
        r["extra"] for r in records_named(records, "llm_usage") if r["extra"]["usage_kind"] == kind
    ]


async def test_a_running_agent_gets_no_suggestion_and_no_model_is_called(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.set_status(AgentStatus.RUNNING)

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "agent_not_idle")
    assert harness.gate.requests == []
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []


async def test_an_unknown_agent_fails_the_request(harness: Harness) -> None:
    with pytest.raises(AgentNotFound):
        await harness.suggest(404)


async def test_the_cluster_switch_turns_the_pipeline_off_without_loading_providers(
    harness: Harness,
) -> None:
    harness.configure(enabled=False)
    harness.catalog = CatalogLoadFailure(RuntimeError("disabled feature must not load providers"))

    result = await harness.suggest()

    assert result.reason == "disabled"
    assert harness.gate.requests == []


async def test_catalog_failure_stays_explicit_and_does_not_call_models(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    failure = RuntimeError("catalog unavailable")
    harness.catalog = CatalogLoadFailure(failure)

    with pytest.raises(ModelCatalogUnavailableError) as caught:
        await harness.suggest()

    assert caught.value.__cause__ is failure
    assert harness.gate.requests == []
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []


async def test_invalid_model_configuration_does_not_replay_a_cached_suggestion(
    harness: Harness,
) -> None:
    await harness.suggest()
    calls = len(harness.gate.requests)
    harness.configure(gate_model="not-a-registered-model")

    with pytest.raises(ReplySuggestionConfigurationError, match="not-a-registered-model"):
        await harness.suggest()

    assert len(harness.gate.requests) == calls


async def test_a_gate_below_the_threshold_returns_no_suggestion(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(gate=GateServer(0.2))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "not_useful")
    assert len(harness.gate.requests) == 1
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []


async def test_a_gate_exactly_at_the_threshold_passes(make_harness: HarnessFactory) -> None:
    harness = make_harness(gate=GateServer(0.5))

    result = await harness.suggest()

    assert result.suggestions == _TWO_REPLIES


async def test_the_gate_asks_its_question_about_the_excerpt_and_is_billed_to_the_agent(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    await harness.suggest()

    [asked] = harness.gate.asked()
    assert asked["model"] == "jev-1.13.0"
    assert asked["state"] == "[User]\nPlease prepare the release.\n\n[Assistant]\nDeploy now?"
    assert list(asked["questions"]) == ["useful_reply"]
    assert asked["questions"]["useful_reply"]["type"] == "noul"
    [bill] = usage_of(loguru_records, GATE_USAGE_KIND)
    assert (bill["model"], bill["agent_id"]) == ("jev-1.13.0", harness.agent_id)
    assert GATE_USAGE_KIND == "reply_suggestion_gate"


async def test_a_passing_gate_drafts_and_logs_the_usage(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(
        chat=DraftChat(
            reply='{"suggestions": ["  Yes, deploy it.  ", "Wait, explain the plan first."]}'
        )
    )

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == (
        ["Yes, deploy it.", "Wait, explain the plan first."],
        None,
    )
    [usage] = usage_of(loguru_records, DRAFT_USAGE_KIND)
    assert (usage["model"], usage["agent_id"], usage["in_total"], usage["out_total"]) == (
        "deepseek-flash",
        harness.agent_id,
        120,
        14,
    )
    assert usage["latency_ms"] >= 0
    assert DRAFT_USAGE_KIND == "reply_suggestion"


async def test_an_oversized_conversation_is_cut_to_the_input_budget(harness: Harness) -> None:
    harness.configure(max_input_tokens=120)
    harness.store(
        [
            SystemMessage(content="system prompt"),
            inbound("question " * 300 + "the-last-message"),
            agent_text("filler " * 600 + "Shall I deploy?"),
        ]
    )

    await harness.suggest()

    [asked] = harness.gate.asked()
    assert len(asked["state"]) < len("question " * 300 + "filler " * 600)
    assert asked["state"].endswith("the-last-message")


async def test_a_completed_result_is_served_from_the_cache(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    first = await harness.suggest()
    second = await harness.suggest()

    assert second == first
    assert len(harness.gate.requests) == 1
    assert len(usage_of(loguru_records, DRAFT_USAGE_KIND)) == 1


async def test_a_gate_refusal_is_cached_too(make_harness: HarnessFactory) -> None:
    harness = make_harness(gate=GateServer(0.1))

    await harness.suggest()
    await harness.suggest()

    assert len(harness.gate.requests) == 1


async def test_a_new_agent_message_is_a_new_cache_entry(harness: Harness) -> None:
    await harness.suggest()
    harness.store([*conversation(), inbound("yes"), agent_text("Done. Anything else?", "ai-2")])

    await harness.suggest()

    assert len(harness.gate.requests) == 2


async def test_two_agents_do_not_share_cache_entries(harness: Harness) -> None:
    other = harness.new_agent()

    await harness.suggest()
    await harness.suggest(other)

    assert len(harness.gate.requests) == 2


async def test_a_gate_failure_degrades_to_no_suggestion_with_a_warning(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(gate=GateServer(_PROVIDER_DOWN))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_gate_failed"]


async def test_a_failure_is_not_cached_so_the_next_request_retries(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(gate=GateServer(_PROVIDER_DOWN, 0.9))
    await harness.suggest()

    result = await harness.suggest()

    assert result.suggestions == _TWO_REPLIES
    assert len(harness.gate.requests) == 2


async def test_a_draft_failure_degrades_to_no_suggestion_with_a_warning(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(chat=DraftChat(error=httpx.ReadTimeout("slow")))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "generation_failed")
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_draft_failed"]


async def test_an_invalid_draft_is_rejected_with_a_warning_and_its_usage_is_still_logged(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(
        chat=DraftChat(reply=json.dumps({"suggestions": ["A.", "B.", "C.", "D."]}))
    )

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    (rejected,) = records_named(loguru_records, "reply_suggestion_draft_rejected")
    assert rejected["extra"]["rejection"] == "too_many"
    assert len(records_named(loguru_records, "llm_usage")) == 2


async def test_a_draft_that_is_not_json_is_rejected(make_harness: HarnessFactory) -> None:
    harness = make_harness(chat=DraftChat(reply="not JSON"))

    assert (await harness.suggest()).reason == "invalid_generation"


@pytest.mark.parametrize(
    "stop",
    [
        {"model_provider": "openai", "status": "incomplete"},
        {"model_provider": "anthropic", "stop_reason": "max_tokens"},
        {"model_provider": "openai", "status": "failed"},
        {"model_provider": "anthropic"},
    ],
)
async def test_unfinished_structured_output_is_rejected_even_if_the_text_is_valid(
    make_harness: HarnessFactory, stop: dict[str, str], loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(chat=DraftChat(stop=stop))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    (rejected,) = records_named(loguru_records, "reply_suggestion_draft_rejected")
    assert rejected["extra"]["rejection"] == "unfinished"


async def test_the_configured_bound_limits_the_candidates(harness: Harness) -> None:
    harness.configure(max_candidates=1)

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")


async def test_a_conversation_without_agent_text_has_nothing_to_suggest(harness: Harness) -> None:
    harness.store([SystemMessage(content="system prompt"), inbound("hello")])

    result = await harness.suggest()

    assert result.reason == "no_agent_text"
    assert harness.gate.requests == []


async def test_an_unclassified_draft_error_is_not_swallowed(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(chat=DraftChat(error=ValueError("a bug, not a provider failure")))

    with pytest.raises(ValueError, match="a bug"):
        await harness.suggest()


async def test_a_missing_gate_key_degrades_like_any_decision_error(harness: Harness) -> None:
    harness.keys = ProviderKeys({})

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.gate.requests == []


async def test_an_error_outside_the_decision_family_is_not_swallowed(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(gate=GateServer(RuntimeError("not a DecisionError")))

    with pytest.raises(RuntimeError, match="not a DecisionError"):
        await harness.suggest()
