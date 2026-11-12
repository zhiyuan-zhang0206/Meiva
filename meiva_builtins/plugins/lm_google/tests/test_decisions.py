# meiva:note
# Provider wire/auth/error contracts against the synthetic live response.

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

import httpx
import pytest

from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    Choice,
    DecisionBuildContext,
    DecisionInputTooLargeError,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    YesNo,
)
from base.lm.catalog.provider_keys import MissingProviderKeyError, ProviderKeys
from base.lm.decision.call import validate_decisions
from meiva_builtins.plugins.lm_google.decision import (
    DECISION_PROVIDER,
    MODEL,
    GeminiDecisionBinding,
)

QUESTIONS = {
    "blue": YesNo("Is the parcel blue?", "Blue", "Not blue"),
    "color": Choice("Color?", {"blue": "Blue", "red": "Red"}),
    "clean": Score("Clean?", ("dirty", "clean"), ("Dirty", "Clean")),
}


class WireResponse(TypedDict):
    answers: dict[str, dict[str, object]]
    usage: dict[str, int]


def response() -> WireResponse:
    return cast(
        WireResponse, json.loads(Path(__file__).with_name("decision_response.json").read_text())
    )


def binding(handler: Callable[[httpx.Request], httpx.Response]) -> GeminiDecisionBinding:
    return GeminiDecisionBinding(
        DecisionBuildContext(
            model=MODEL,
            spec=DECISION_PROVIDER.models[MODEL],
            timeout=1,
            keys=ProviderKeys({"GEMINI_API_KEY": "test-key"}),
            transport=httpx.MockTransport(handler),
        )
    )


async def test_native_schema_auth_and_all_question_types() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "authorization" not in request.headers
        body = cast(dict[str, object], json.loads(request.content))
        assert body == {
            "model": MODEL,
            "input": [{"type": "text", "text": "A blue parcel."}],
            "questions": {
                "blue": {
                    "type": "noul",
                    "instructions": "Is the parcel blue?",
                    "criteria": {"true_description": "Blue", "false_description": "Not blue"},
                },
                "color": {
                    "type": "choice",
                    "instructions": "Color?",
                    "criteria": {"blue": "Blue", "red": "Red"},
                },
                "clean": {
                    "type": "score",
                    "instructions": "Clean?",
                    "criteria": ["dirty: Dirty", "clean: Clean"],
                },
            },
        }
        return httpx.Response(200, json=response())

    result = await binding(handle).decide("A blue parcel.", QUESTIONS)
    validate_decisions(QUESTIONS, result.decisions)
    assert len(calls) == 1
    assert result.input_tokens == 225
    assert result.decisions["blue"].probability("true") > 0.9
    assert result.decisions["color"].top == "blue"
    assert result.decisions["clean"].top == "clean"
    score = result.decisions["clean"].score
    assert score is not None and score > 0.8


@pytest.mark.parametrize("kind", ["empty", "missing", "extra", "out_of_range", "wrong_type"])
async def test_bad_answers_fail_without_losing_billed_input(kind: str) -> None:
    body = response()
    if kind == "empty":
        body["answers"] = {}
    elif kind == "missing":
        del body["answers"]["blue"]
    elif kind == "extra":
        body["answers"]["other"] = body["answers"]["blue"]
    elif kind == "out_of_range":
        body["answers"]["blue"]["noul"] = 2
    else:
        body["answers"]["blue"]["type"] = "choice"
    try:
        result = await binding(lambda _: httpx.Response(200, json=body)).decide("text", QUESTIONS)
    except DecisionResponseError as exc:
        assert exc.input_tokens == 225
    else:
        with pytest.raises(DecisionResponseError):
            validate_decisions(QUESTIONS, result.decisions)


async def test_error_status_is_not_retried_or_parsed_as_a_decision() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": "denied"})

    with pytest.raises(DecisionRequestError, match="HTTP 401"):
        await binding(handle).decide("text", QUESTIONS)
    assert len(calls) == 1


async def test_question_limit_fails_before_request() -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network was called")

    with pytest.raises(DecisionInputTooLargeError, match="1000"):
        await binding(unexpected).decide("text", {str(n): QUESTIONS["blue"] for n in range(1001)})


def test_key_is_required_before_any_call() -> None:
    with pytest.raises(MissingProviderKeyError):
        GeminiDecisionBinding(
            DecisionBuildContext(
                model=MODEL, spec=DECISION_PROVIDER.models[MODEL], timeout=1, keys=ProviderKeys({})
            )
        )


def test_decision_is_registered_separately_from_chat(model_catalog: ModelCatalog) -> None:
    assert MODEL in model_catalog.decision_models
    assert MODEL not in model_catalog.models
    provider = model_catalog.decision_provider_of(MODEL)
    assert provider is not None
    assert provider.key_env == "GEMINI_API_KEY"


def test_new_defaults_keep_explicit_decision_pins_and_chat_selection() -> None:
    from base.config.domains.agent.settings import AgentSettings
    from base.config.domains.lm import LmSettings
    from base.config.domains.services.settings import ServiceSettings

    assert AgentSettings().skill_recall_model == MODEL
    assert ServiceSettings().memory_rerank_model == MODEL
    assert LmSettings().reply_suggestion_gate_model == MODEL
    assert LmSettings().llm_model == "deepseek-flash"
    assert (
        AgentSettings.model_validate({"MEIVA_SKILL_RECALL_MODEL": "jev-1.13.0"}).skill_recall_model
        == "jev-1.13.0"
    )
