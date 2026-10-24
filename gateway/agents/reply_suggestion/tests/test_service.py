# meiva:note
# The suggestion service against a real store: it reads the agent, gates, drafts and remembers.
#
# The judgments are asserted in `test_plan.py`; here the agent, its status and its conversation are
# rows in throwaway Postgres. The real Jev binding and LangChain adapter talk to a local HTTP
# listener. Each outcome is read from the result, log and persisted HTTP request receipts.

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import SystemMessage

from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.tests.model_catalog import inert_keys
from gateway.agents.reply_suggestion.service import (
    DRAFT_USAGE_KIND,
    GATE_USAGE_KIND,
    ReplySuggestionConfigurationError,
)
from gateway.agents.reply_suggestion.tests.provider_wire import DraftReply, GateReply, ProviderWire
from gateway.agents.reply_suggestion.tests.support import (
    Harness,
    agent_text,
    conversation,
    inbound,
)
from gateway.http.model_catalog import CatalogLoadFailure, ModelCatalogUnavailableError

HarnessFactory = Callable[..., Harness]
_PROVIDER_DOWN = GateReply(status=500, body='{"error": "provider down"}')
_TWO_REPLIES = ["Yes, go ahead.", "Wait, explain the plan first."]


@pytest.fixture
def make_harness(
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog, tmp_path: Path
) -> Iterator[HarnessFactory]:
    with ExitStack() as resources:
        ordinal = 0

        def make(
            *,
            gate: tuple[GateReply, ...] = (GateReply(),),
            draft: DraftReply | None = None,
            keys: ProviderKeys | None = None,
        ) -> Harness:
            nonlocal ordinal
            directory = tmp_path / str(ordinal)
            ordinal += 1
            wire = resources.enter_context(
                ProviderWire(directory, gate, DraftReply() if draft is None else draft)
            )
            return Harness(
                db_conn,
                database,
                model_catalog,
                wire=wire,
                keys=inert_keys(model_catalog) if keys is None else keys,
            )

        yield make


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
    assert harness.wire.gate_receipts() == []
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
    assert harness.wire.gate_receipts() == []


async def test_catalog_failure_stays_explicit_and_does_not_call_models(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    failure = RuntimeError("catalog unavailable")
    harness.catalog = CatalogLoadFailure(failure)

    with pytest.raises(ModelCatalogUnavailableError) as caught:
        await harness.suggest()

    assert caught.value.__cause__ is failure
    assert harness.wire.gate_receipts() == []
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []


async def test_invalid_model_configuration_does_not_replay_a_cached_suggestion(
    harness: Harness,
) -> None:
    await harness.suggest()
    calls = len(harness.wire.gate_receipts())
    harness.configure(gate_model="not-a-registered-model")

    with pytest.raises(ReplySuggestionConfigurationError, match="not-a-registered-model"):
        await harness.suggest()

    assert len(harness.wire.gate_receipts()) == calls


async def test_a_gate_below_the_threshold_returns_no_suggestion(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(gate=(GateReply(0.2),))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "not_useful")
    assert len(harness.wire.gate_receipts()) == 1
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []


async def test_a_gate_exactly_at_the_threshold_passes(make_harness: HarnessFactory) -> None:
    harness = make_harness(gate=(GateReply(0.5),))

    result = await harness.suggest()

    assert result.suggestions == _TWO_REPLIES


async def test_the_gate_asks_its_question_about_the_excerpt_and_is_billed_to_the_agent(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    await harness.suggest()

    [asked] = harness.wire.gate_receipts()
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
        draft=DraftReply(
            text='{"suggestions": ["  Yes, deploy it.  ", "Wait, explain the plan first."]}'
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
    [sent] = harness.wire.draft_receipts()
    assert sent["model"] == "deepseek-flash"
    assert sent["reasoning"] == {"effort": "none"}
    output_format = sent["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["required"] == ["suggestions"]
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

    [asked] = harness.wire.gate_receipts()
    assert len(asked["state"]) < len("question " * 300 + "filler " * 600)
    assert asked["state"].endswith("the-last-message")


async def test_a_completed_result_is_served_from_the_cache(
    harness: Harness, loguru_records: list[dict[str, Any]]
) -> None:
    first = await harness.suggest()
    second = await harness.suggest()

    assert second == first
    assert len(harness.wire.gate_receipts()) == 1
    assert len(usage_of(loguru_records, DRAFT_USAGE_KIND)) == 1


async def test_a_gate_refusal_is_cached_too(make_harness: HarnessFactory) -> None:
    harness = make_harness(gate=(GateReply(0.1),))

    await harness.suggest()
    await harness.suggest()

    assert len(harness.wire.gate_receipts()) == 1


async def test_a_new_agent_message_is_a_new_cache_entry(harness: Harness) -> None:
    await harness.suggest()
    harness.store([*conversation(), inbound("yes"), agent_text("Done. Anything else?", "ai-2")])

    await harness.suggest()

    assert len(harness.wire.gate_receipts()) == 2


async def test_two_agents_do_not_share_cache_entries(harness: Harness) -> None:
    other = harness.new_agent()

    await harness.suggest()
    await harness.suggest(other)

    assert len(harness.wire.gate_receipts()) == 2


async def test_a_gate_failure_degrades_to_no_suggestion_with_a_warning(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(gate=(_PROVIDER_DOWN,))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert usage_of(loguru_records, DRAFT_USAGE_KIND) == []
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_gate_failed"]


async def test_a_failure_is_not_cached_so_the_next_request_retries(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(gate=(_PROVIDER_DOWN, GateReply(0.9)))
    await harness.suggest()

    result = await harness.suggest()

    assert result.suggestions == _TWO_REPLIES
    assert len(harness.wire.gate_receipts()) == 2


async def test_a_draft_failure_degrades_to_no_suggestion_with_a_warning(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(draft=DraftReply(http_status=503))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "generation_failed")
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert [r["extra"]["event"] for r in warnings] == ["reply_suggestion_draft_failed"]


async def test_an_invalid_draft_is_rejected_with_a_warning_and_its_usage_is_still_logged(
    make_harness: HarnessFactory, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(
        draft=DraftReply(text=json.dumps({"suggestions": ["A.", "B.", "C.", "D."]}))
    )

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "invalid_generation")
    (rejected,) = records_named(loguru_records, "reply_suggestion_draft_rejected")
    assert rejected["extra"]["rejection"] == "too_many"
    assert len(records_named(loguru_records, "llm_usage")) == 2


async def test_a_draft_that_is_not_json_is_rejected(make_harness: HarnessFactory) -> None:
    harness = make_harness(draft=DraftReply(text="not JSON"))

    assert (await harness.suggest()).reason == "invalid_generation"


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
async def test_unfinished_structured_output_is_rejected_even_if_the_text_is_valid(
    make_harness: HarnessFactory, status: str, loguru_records: list[dict[str, Any]]
) -> None:
    harness = make_harness(draft=DraftReply(status=status))
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
    assert harness.wire.gate_receipts() == []


async def test_a_missing_gate_key_degrades_like_any_decision_error(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(keys=ProviderKeys({}))

    result = await harness.suggest()

    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert harness.wire.gate_receipts() == []


@pytest.mark.parametrize("body", ["not JSON", "[]", '{"answers": {}}'])
async def test_malformed_gate_responses_degrade_without_drafting(
    make_harness: HarnessFactory, body: str
) -> None:
    harness = make_harness(gate=(GateReply(body=body),))
    result = await harness.suggest()
    assert (result.suggestions, result.reason) == ([], "gate_failed")
    assert len(harness.wire.gate_receipts()) == 1
    assert not harness.wire.started.is_set()


async def test_an_unclassified_adapter_error_is_not_swallowed(make_harness: HarnessFactory) -> None:
    harness = make_harness(draft=DraftReply(status="failed", error_message="provider protocol bug"))
    with pytest.raises(ValueError, match="provider protocol bug"):
        await harness.suggest()
