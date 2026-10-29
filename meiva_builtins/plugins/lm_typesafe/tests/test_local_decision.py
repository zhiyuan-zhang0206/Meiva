from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.lm.catalog.decide.types import (
    Choice,
    DecisionInputTooLargeError,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    UnknownDecisionModelError,
    YesNo,
)
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.decision.call import decide
from base.lm.pricing.archive import load_archive
from meiva_builtins.plugins.lm_typesafe.default_config import LocalDecisionConfig
from meiva_builtins.plugins.lm_typesafe.local_provider import local_contribution

MODEL = "local-decision-laya-abc123"
ENDPOINT = "http://127.0.0.1:8000/v1/systemone"
QUESTIONS = {
    "remember": YesNo("Is this a repeated user requirement?", "repeated", "one-off"),
    "team": Choice("Which project?", {"memory": "recall", "ci": "builds"}),
    "urgency": Score("Urgency?", ("low", "high"), ("later", "now")),
}


def config_values() -> dict[str, Any]:
    return {
        "local_endpoint": ENDPOINT,
        "local_models": {
            MODEL: {
                "wire_model": "multilingual",
                "response_model": "laya-rl-agent",
                "revision": "a" * 40,
                "context_window": 1_024,
                "max_request_tokens": 2_048,
            }
        },
    }


def catalog(values: dict[str, Any] | None = None) -> ModelCatalog:
    contribution = local_contribution(LocalDecisionConfig.model_validate(values or config_values()))
    builder = CatalogBuilder(load_archive())
    builder.install_decision("lm_typesafe", contribution)
    return builder.build()


def body() -> dict[str, Any]:
    return {
        "model": "laya-rl-agent",
        "routing": {"model": "multilingual"},
        "answers": {
            "remember": {"type": "noul", "noul": 0.87},
            "team": {
                "type": "choice",
                "choice": "memory",
                "confidence": 0.45,
                "probabilities": {"memory": 0.8, "ci": 0.2},
            },
            "urgency": {
                "type": "score",
                "confidence": 0.35,
                "score": 0.6,
                "probabilities": {"0": 0.4, "1": 0.6},
            },
        },
        "usage": {
            "input_tokens": 96,
            "output_tokens": 0,
            "truncated": False,
            "state_tokens_dropped": 0,
            "truncated_questions": [],
        },
    }


async def call(
    transport: httpx.MockTransport,
    *,
    selected: ModelCatalog | None = None,
    keys: ProviderKeys | None = None,
    timeout: float | None = 1,
    model: str = MODEL,
    state: str = "The user repeated the CI speed requirement.",
) -> Any:
    return await decide(
        model,
        state,
        QUESTIONS,
        catalog=selected or catalog(),
        keys=keys or ProviderKeys({}),
        usage_kind="local_decision_test",
        transport=transport,
        timeout=timeout,
    )


@pytest.mark.parametrize("authenticated", [False, True])
async def test_typed_wire_and_optional_real_auth(authenticated: bool) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body())

    held = {"TYPESAFE_API_KEY": "unrelated-jev-key"}
    if authenticated:
        held["LOCAL_DECISION_API_KEY"] = "real-local-key"
    result = await call(httpx.MockTransport(respond), keys=ProviderKeys(held))
    [request] = requests
    assert str(request.url) == ENDPOINT
    assert request.method == "POST"
    assert request.headers.get("authorization") == (
        "Bearer real-local-key" if authenticated else None
    )
    assert json.loads(request.content) == {
        "model": "multilingual",
        "state": "The user repeated the CI speed requirement.",
        "questions": {
            "remember": {
                "type": "noul",
                "instructions": QUESTIONS["remember"].question,
                "criteria": {"true": "repeated", "false": "one-off"},
            },
            "team": {
                "type": "choice",
                "instructions": "Which project?",
                "criteria": {"memory": "recall", "ci": "builds"},
            },
            "urgency": {
                "type": "score",
                "instructions": "Urgency?",
                "criteria": ["low: later", "high: now"],
            },
        },
    }
    assert result["remember"].probabilities == pytest.approx({"true": 0.87, "false": 0.13})
    assert result["team"].top == "memory"
    assert result["team"].confidence == 0.45
    assert result["urgency"].probabilities == {"low": 0.4, "high": 0.6}
    assert result["urgency"].score == 0.6


@pytest.mark.parametrize("status", [401, 403, 422, 429, 503])
async def test_service_failures_are_one_attempt(status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": "service refused"})

    with pytest.raises(DecisionRequestError, match=f"HTTP {status}"):
        await call(httpx.MockTransport(respond))
    assert len(requests) == 1


async def test_total_deadline_cancels_one_slow_request() -> None:
    requests: list[httpx.Request] = []
    cancelled = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(200, json=body())

    with pytest.raises(DecisionRequestError, match="deadline exceeded"):
        await call(httpx.MockTransport(respond), timeout=0.02)
    assert len(requests) == 1
    assert cancelled.is_set()


_Mutate = Callable[[dict[str, Any]], object]


_BAD_RESPONSES: list[tuple[_Mutate, str]] = [
    (lambda b: b.update(model="wrong-model"), "response model"),
    (lambda b: b.update(routing={"model": "english"}), "different model route"),
    (lambda b: b.update(routing="multilingual"), "different model route"),
    (lambda b: b["answers"].pop("team"), "missing"),
    (lambda b: b["answers"].update(ghost={"type": "noul", "noul": 0.4}), "unknown question"),
    (lambda b: b["answers"]["remember"].update(noul=True), "not a number"),
    (lambda b: b["answers"]["remember"].update(noul=float("nan")), "not finite"),
    (lambda b: b["answers"]["remember"].update(noul=1.1), "outside"),
    (lambda b: b["answers"]["team"].update(choice="ci"), "most probable"),
    (lambda b: b["answers"]["team"].pop("confidence"), "not a number"),
    (lambda b: b["answers"]["team"]["probabilities"].update(ci=0.7), "sum to"),
    (lambda b: b["answers"]["team"]["probabilities"].pop("ci"), "missing"),
    (lambda b: b["answers"]["urgency"].update(probabilities={"-1": 1.0}), "level key"),
    (lambda b: b["answers"]["urgency"].update(probabilities={"01": 1.0}), "level key"),
    (lambda b: b["answers"]["urgency"].update(probabilities={}), "no score probabilities"),
    (lambda b: b["answers"]["urgency"].update(score=2), "outside"),
    (lambda b: b["usage"].update(truncated=True), "truncated"),
    (lambda b: b["usage"].update(state_tokens_dropped=4), "truncated"),
    (lambda b: b["usage"].update(truncated_questions=["remember"]), "truncated"),
    (lambda b: b["usage"].update(options={"team": {"distinct": 1, "total": 2}}), "collapsed"),
]


@pytest.mark.parametrize(("mutate", "match"), _BAD_RESPONSES)
async def test_invalid_responses_preserve_billed_usage(
    mutate: _Mutate,
    match: str,
    loguru_records: list[dict[str, Any]],
) -> None:
    response = body()
    mutate(response)
    with pytest.raises(DecisionResponseError, match=match):
        await call(httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(response))))
    [usage] = [r["extra"] for r in loguru_records if r["extra"].get("event") == "llm_usage"]
    assert usage["model"] == MODEL
    assert usage["in_total"] == 96
    assert usage["out_total"] == 0
    assert usage["cost_usd"] == 0


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True}, {"input_tokens": -1}])
async def test_missing_usage_is_never_invented(
    usage: object, loguru_records: list[dict[str, Any]]
) -> None:
    response = body()
    response["usage"] = usage
    with pytest.raises(DecisionResponseError, match=r"usage\.input_tokens"):
        await call(httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    assert not [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]


@pytest.mark.parametrize("mode", ["unknown", "overflow", "unsupported", "choice_cap", "score_cap"])
async def test_invalid_requests_fail_before_http(mode: str) -> None:
    values = config_values()
    row = values["local_models"][MODEL]
    if mode == "unsupported":
        row["question_types"] = ["noul"]
    if mode == "choice_cap":
        row["max_choice_options"] = 2
    if mode == "score_cap":
        row["max_score_levels"] = 2
    requests: list[httpx.Request] = []
    selected = catalog(values)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body())

    if mode == "choice_cap":
        questions = {"team": Choice("Project?", {"a": "A", "b": "B", "c": "C"})}
    elif mode == "score_cap":
        questions = {"score": Score("Level?", ("a", "b", "c"), ("A", "B", "C"))}
    else:
        questions = QUESTIONS
    with pytest.raises(
        UnknownDecisionModelError
        if mode == "unknown"
        else DecisionInputTooLargeError
        if mode == "overflow"
        else ValueError
    ):
        await decide(
            "local-decision-unknown" if mode == "unknown" else MODEL,
            "x" * 10_000 if mode == "overflow" else "hello",
            questions,
            catalog=selected,
            keys=ProviderKeys({}),
            usage_kind="test",
            transport=httpx.MockTransport(respond),
        )
    assert requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("local_endpoint", "http://user:secret@localhost:8000/v1/systemone"),
        ("local_endpoint", "http://localhost:8000/v1/chat/completions"),
        ("local_endpoint", "http://localhost:8000/v1/systemone?token=secret"),
        ("local_endpoint", "http://localhost:70000/v1/systemone"),
        ("local_endpoint", " http://localhost:8000/v1/systemone"),
        ("local_endpoint", "http://localhost:0/v1/systemone"),
        ("local_models", {"chat-model": {}}),
        ("unknown", True),
    ],
)
def test_bad_config_fails_fast(field: str, value: object) -> None:
    values = config_values()
    values[field] = value
    with pytest.raises(ValidationError):
        LocalDecisionConfig.model_validate(values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "latest"),
        ("context_window", 0),
        ("context_window", True),
        ("max_request_tokens", 10),
        ("question_types", ["rerank"]),
        ("question_types", []),
        ("question_types", ["noul", "noul"]),
        ("response_model", " "),
        ("wire_model", "multilingual "),
        ("unknown", 42),
    ],
)
def test_bad_model_declarations_fail_fast(field: str, value: object) -> None:
    values = config_values()
    values["local_models"][MODEL][field] = value
    with pytest.raises(ValidationError):
        LocalDecisionConfig.model_validate(values)


def test_catalog_snapshot_isolated_and_zero_price_is_api_charge_only() -> None:
    config = LocalDecisionConfig.model_validate(config_values())
    contribution = local_contribution(config)
    config.local_models.clear()
    builder = CatalogBuilder(load_archive())
    builder.install_decision("lm_typesafe", contribution)
    selected = builder.build()
    assert MODEL in selected.decision_models
    assert MODEL not in selected.models
    assert selected.key_envs == ("LOCAL_DECISION_API_KEY",)
    rates = selected.prices.rates_at(MODEL, None, 96)
    assert rates is not None
    assert rates.as_tuple() == (0, 0, 0)


@pytest.mark.parametrize("raw", ["not json", "[]", "null"])
async def test_non_object_responses_fail(raw: str) -> None:
    with pytest.raises(DecisionResponseError):
        await call(httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))


async def test_transport_error_is_not_retried() -> None:
    attempts = 0

    def refuse(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(DecisionRequestError, match="ConnectError"):
        await call(httpx.MockTransport(refuse))
    assert attempts == 1


async def test_standard_systemone_without_truncation_receipt_is_not_claimed_verified() -> None:
    response = body()
    response["usage"] = {"input_tokens": 96, "output_tokens": 0}
    response.pop("routing")
    result = await call(httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    assert result["remember"].probability("true") == 0.87
