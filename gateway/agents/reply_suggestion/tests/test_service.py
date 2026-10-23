# meiva:note
# The suggestion service against a real store: it reads the agent, gates, drafts and remembers.
#
# The judgments are asserted in `test_plan.py`; here the agent, its status and its conversation are
# rows in the throwaway Postgres, and only the two model calls are doubles.

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    DecisionKeyMissingError,
    DecisionRequestError,
    YesNo,
    estimate_tokens,
)
from gateway.agents.reply_suggestion.plan import draft_instruction
from gateway.agents.reply_suggestion.schemas import DraftReplies
from gateway.agents.reply_suggestion.service import (
    DRAFT_USAGE_KIND,
    GATE_USAGE_KIND,
    ReplySuggestionConfigurationError,
)
from gateway.agents.reply_suggestion.tests.support import (
    Harness,
    agent_text,
    conversation,
    inbound,
    useful_reply,
)
from gateway.http.model_catalog import CatalogLoadFailure, ModelCatalogUnavailableError


@pytest.fixture
def harness(
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog
) -> Harness:
    return Harness(db_conn, database, model_catalog)


def records_named(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == event]


async def test_a_running_agent_gets_no_suggestion_and_no_model_is_called(harness: Harness) -> None:
    harness.set_status(AgentStatus.RUNNING)

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "agent_not_idle")
    assert harness.decide.calls == []
    assert harness.chat.invocations == []


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
    assert harness.decide.calls == []


async def test_catalog_failure_stays_explicit_and_does_not_call_models(harness: Harness) -> None:
    failure = RuntimeError("catalog unavailable")
    harness.catalog = CatalogLoadFailure(failure)

    with pytest.raises(ModelCatalogUnavailableError) as caught:
        await harness.suggest()

    assert caught.value.__cause__ is failure
    assert harness.decide.calls == []
    assert harness.chat.invocations == []


async def test_invalid_model_configuration_does_not_replay_a_cached_suggestion(
    harness: Harness,
) -> None:
    await harness.suggest()
    calls = len(harness.decide.calls)
    harness.configure(gate_model="not-a-registered-model")

    with pytest.raises(ReplySuggestionConfigurationError, match="not-a-registered-model"):
        await harness.suggest()

    assert len(harness.decide.calls) == calls


async def test_a_gate_below_the_threshold_returns_no_suggestion(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.2)

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "not_useful")
    assert len(harness.decide.calls) == 1
    assert harness.chat.invocations == []


async def test_a_gate_exactly_at_the_threshold_passes(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.5)

    result = await harness.suggest()

    assert result.suggestions == ["Yes, go ahead.", "Wait, explain the plan first."]


async def test_the_gate_asks_its_question_about_the_excerpt_and_is_billed_to_the_agent(
    harness: Harness,
) -> None:
    await harness.suggest()

    model, state, questions, kwargs = harness.decide.calls[0]
    assert model == "jev-1.13.0"
    assert state == "[User]\nPlease prepare the release.\n\n[Assistant]\nDeploy now?"
    assert list(questions) == ["useful_reply"]
    assert isinstance(questions["useful_reply"], YesNo)
    assert kwargs["usage_kind"] == GATE_USAGE_KIND == "reply_suggestion_gate"
    assert kwargs["for_agent_id"] == harness.agent_id
    assert kwargs["catalog"] is harness.catalog


async def test_a_passing_gate_drafts_with_the_instruction_first_and_logs_the_usage(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.reply = '{"suggestions": ["  Yes, deploy it.  ", "Wait, explain the plan first."]}'

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == (
        ["Yes, deploy it.", "Wait, explain the plan first."],
        None,
    )
    assert harness.chat.built == ["deepseek-flash"]
    assert harness.chat.bindings == [
        (
            DraftReplies.model_json_schema(),
            {"method": "json_schema", "strict": True, "include_raw": True},
        )
    ]
    (messages,) = harness.chat.invocations
    assert [type(m) for m in messages] == [SystemMessage, HumanMessage]
    assert messages[0].content == draft_instruction(3)
    assert messages[1].content == harness.decide.calls[0][1]
    (usage,) = [
        r["extra"]
        for r in records_named(loguru_records, "llm_usage")
        if r["extra"]["usage_kind"] == DRAFT_USAGE_KIND
    ]
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

    state = harness.decide.calls[0][1]
    assert estimate_tokens(state) <= 120
    assert state.endswith("the-last-message")
    assert harness.chat.invocations[0][1].content == state


async def test_a_completed_result_is_served_from_the_cache(harness: Harness) -> None:
    first = await harness.suggest()
    second = await harness.suggest()

    assert second == first
    assert len(harness.decide.calls) == 1
    assert len(harness.chat.invocations) == 1


async def test_a_gate_refusal_is_cached_too(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.1)

    await harness.suggest()
    await harness.suggest()

    assert len(harness.decide.calls) == 1


async def test_a_new_agent_message_is_a_new_cache_entry(harness: Harness) -> None:
    await harness.suggest()
    harness.store([*conversation(), inbound("yes"), agent_text("Done. Anything else?", "ai-2")])

    await harness.suggest()

    assert len(harness.decide.calls) == 2


async def test_two_agents_do_not_share_cache_entries(harness: Harness) -> None:
    other = harness.new_agent()

    await harness.suggest()
    await harness.suggest(other)

    assert len(harness.decide.calls) == 2


async def test_a_gate_failure_degrades_to_no_suggestion_with_a_warning(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.decide.error = DecisionRequestError("provider down")

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.chat.invocations == []
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_gate_failed"]


async def test_a_failure_is_not_cached_so_the_next_request_retries(harness: Harness) -> None:
    harness.decide.error = DecisionRequestError("provider down")
    await harness.suggest()
    harness.decide.error = None

    result = await harness.suggest()

    assert result.suggestions == ["Yes, go ahead.", "Wait, explain the plan first."]
    assert len(harness.decide.calls) == 2


async def test_a_draft_failure_degrades_to_no_suggestion_with_a_warning(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.error = httpx.ReadTimeout("slow")

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "generation_failed")
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_draft_failed"]


async def test_an_invalid_draft_is_rejected_with_a_warning_and_its_usage_is_still_logged(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.reply = json.dumps({"suggestions": ["A.", "B.", "C.", "D."]})

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    (rejected,) = records_named(loguru_records, "reply_suggestion_draft_rejected")
    assert rejected["extra"]["rejection"] == "too_many"
    assert len(records_named(loguru_records, "llm_usage")) == 1
    harness.chat.reply = "not JSON"
    assert (await harness.suggest(harness.new_agent())).reason == "invalid_generation"


@pytest.mark.parametrize(
    "metadata",
    [
        {"model_provider": "openai", "status": "incomplete"},
        {"model_provider": "anthropic", "stop_reason": "max_tokens"},
        {"model_provider": "openai", "status": "failed"},
        {"model_provider": "anthropic"},
    ],
)
async def test_unfinished_structured_output_is_rejected_even_if_the_text_is_valid(
    harness: Harness, metadata: dict[str, str], loguru_records: list[dict[str, Any]]
) -> None:
    harness.chat.metadata = metadata

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    (rejected,) = records_named(loguru_records, "reply_suggestion_draft_rejected")
    assert rejected["extra"]["rejection"] == "unfinished"


async def test_the_configured_bound_reaches_the_draft_instruction(harness: Harness) -> None:
    harness.configure(max_candidates=2)

    await harness.suggest()

    (messages,) = harness.chat.invocations
    assert messages[0].content == draft_instruction(2)


async def test_a_conversation_without_agent_text_has_nothing_to_suggest(harness: Harness) -> None:
    harness.store([SystemMessage(content="system prompt"), inbound("hello")])

    result = await harness.suggest()

    assert result.reason == "no_agent_text"
    assert harness.decide.calls == []


async def test_an_unclassified_draft_error_is_not_swallowed(harness: Harness) -> None:
    harness.chat.error = ValueError("a bug, not a provider failure")

    with pytest.raises(ValueError, match="a bug"):
        await harness.suggest()


async def test_a_missing_gate_key_degrades_like_any_decision_error(harness: Harness) -> None:
    harness.decide.error = DecisionKeyMissingError("TYPESAFE_API_KEY is not set")

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.chat.invocations == []


async def test_an_error_outside_the_decision_family_is_not_swallowed(harness: Harness) -> None:
    harness.decide.error = RuntimeError("not a DecisionError")

    with pytest.raises(RuntimeError, match="not a DecisionError"):
        await harness.suggest()
