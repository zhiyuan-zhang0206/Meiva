# meiva:note
# Provider wire/auth/error contracts and skill_recall consumer integration over GeminiServer.
# meiva:link base/lm/docs/decision-models.md

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import httpx
import pytest

from agent.graph.prompt.capabilities import recall_candidates
from agent.graph.prompt.skill_selection import SkillView
from agent.graph.recall.skill_recall import recall_skills
from agent.messages import InboundTimes, inbound_message
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.config.tests.configuration import defaults
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    Choice,
    DecisionBinding,
    DecisionBuildContext,
    DecisionInputTooLargeError,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    YesNo,
)
from base.lm.catalog.provider_keys import MissingProviderKeyError, ProviderKeys
from base.lm.decision.call import validate_decisions
from base.log.tests.log_records import LogRecords
from base.packages.skills.tree import Skill
from base.packages.tests.model_catalog import inert_keys
from meiva_builtins.plugins.lm_google.decision import (
    DECISION_PROVIDER,
    MODEL,
    GeminiDecisionBinding,
)
from meiva_builtins.plugins.lm_google.tests.gemini_server import GeminiServer

QUESTIONS = {
    "blue": YesNo("Is the parcel blue?", "Blue", "Not blue"),
    "color": Choice("Color?", {"blue": "Blue", "red": "Red"}),
    "clean": Score("Clean?", ("dirty", "clean"), ("Dirty", "Clean")),
}

_WIRE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class WireResponse(TypedDict):
    answers: dict[str, dict[str, object]]
    usage: dict[str, int]


def response() -> WireResponse:
    raw = object_of(
        json.loads, Path(__file__).with_name("decision_response.json").read_text(encoding="utf-8")
    )
    assert is_object_dict(raw)
    raw_answers = raw["answers"]
    raw_usage = raw["usage"]
    assert is_object_dict(raw_answers) and is_object_dict(raw_usage)
    answers: dict[str, dict[str, object]] = {}
    for key, val in raw_answers.items():
        assert is_object_dict(val)
        answers[key] = dict(val)
    usage = {key: val for key, val in raw_usage.items() if isinstance(val, int)}
    return {"answers": answers, "usage": usage}


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


def server_binding(server: GeminiServer) -> GeminiDecisionBinding:
    return GeminiDecisionBinding(
        DecisionBuildContext(
            model=MODEL,
            spec=DECISION_PROVIDER.models[MODEL],
            timeout=2,
            keys=ProviderKeys({"GEMINI_API_KEY": "test-key"}),
            transport=server.transport(),
        )
    )


async def test_native_schema_auth_and_all_question_types() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "authorization" not in request.headers
        body = object_of(json.loads, request.content)
        assert body == {
            "model": MODEL,
            "input": [{"type": "text", "text": "A blue parcel."}],
            "questions": {
                "q0": {
                    "type": "noul",
                    "instructions": "Is the parcel blue?",
                    "criteria": {"true_description": "Blue", "false_description": "Not blue"},
                },
                "q1": {
                    "type": "choice",
                    "instructions": "Color?",
                    "criteria": {"blue": "Blue", "red": "Red"},
                },
                "q2": {
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
    assert list(result.decisions) == ["blue", "color", "clean"]
    assert result.decisions["blue"].probability("true") > 0.9
    assert result.decisions["color"].top == "blue"
    assert result.decisions["clean"].top == "clean"
    score = result.decisions["clean"].score
    assert score is not None and score > 0.8


async def test_domain_question_ids_map_bijectively_without_collision(
    gemini_server: GeminiServer,
) -> None:
    long_prefix = "meiva-serious-research:practices:" + ("x" * 50)
    caller_keys = [
        "meiva-guide:mcp",
        "meiva-guide_mcp",
        "meiva-serious-research:practices:measure",
        "docs/lm/decision.v1.md",
        f"{long_prefix}:alpha",
        f"{long_prefix}:beta",
        "q1",
        "q0",
    ]
    assert len(caller_keys[4]) > 64 and len(caller_keys[5]) > 64
    questions = {
        key: YesNo(f"Question for {key}?", "Applies", "Does not apply") for key in caller_keys
    }
    expected_probabilities = {
        key: round(0.10 * (index + 1), 2) for index, key in enumerate(caller_keys)
    }
    gemini_server.answer_decisions(
        {
            f"q{index}": {
                "type": "noul",
                "noul": expected_probabilities[key],
                "confidence": max(expected_probabilities[key], 1.0 - expected_probabilities[key]),
            }
            for index, key in enumerate(caller_keys)
        },
        input_tokens=310,
    )

    result = await server_binding(gemini_server).decide("Evaluate candidate skills.", questions)
    validate_decisions(questions, result.decisions)

    [received] = gemini_server.received
    assert received.path == "/v1beta/decisions"
    wire_questions = received.body["questions"]
    assert is_object_dict(wire_questions)
    assert list(wire_questions) == [f"q{index}" for index in range(len(caller_keys))]
    assert all(_WIRE_ID_RE.fullmatch(qid) for qid in wire_questions)
    assert list(result.decisions) == caller_keys
    for key, expected in expected_probabilities.items():
        assert result.decisions[key].probability("true") == pytest.approx(expected)


@pytest.mark.parametrize(
    "bad_qid",
    [
        "meiva-guide:mcp",
        "docs/lm/decision-models.md",
        "domain.subskill",
        "a" * 65,
        "",
    ],
)
async def test_gemini_server_rejects_invalid_wire_question_ids_with_http_400(
    gemini_server: GeminiServer, bad_qid: str
) -> None:
    gemini_server.answer_decisions(
        {"q0": {"type": "noul", "noul": 0.8, "confidence": 0.8}}, input_tokens=50
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{gemini_server.url}/v1beta/decisions",
            json={
                "model": MODEL,
                "input": [{"type": "text", "text": "state"}],
                "questions": {
                    bad_qid: {
                        "type": "noul",
                        "instructions": "Is it valid?",
                        "criteria": {"true_description": "Yes", "false_description": "No"},
                    }
                },
            },
        )
    assert resp.status_code == 400
    assert "INVALID_ARGUMENT" in resp.text
    if bad_qid:
        assert f"CreateDecisionRequest.questions[{bad_qid}]" in resp.text
        assert "1-64 characters matching [A-Za-z0-9_-]+" in resp.text


@pytest.mark.parametrize(
    ("kind", "expected_fragment"),
    [
        ("empty", "'blue' (wire 'q0')"),
        ("missing", "'blue' (wire 'q0')"),
        ("extra", "unknown ['other']"),
        ("unmapped_caller_id", "unknown ['meiva-guide:mcp']"),
        ("out_of_range", "blue"),
        ("wrong_type", "'blue' (wire 'q0')"),
    ],
)
async def test_bad_answers_fail_without_losing_billed_input(
    kind: str, expected_fragment: str
) -> None:
    body = response()
    if kind == "empty":
        body["answers"] = {}
    elif kind == "missing":
        del body["answers"]["q0"]
    elif kind == "extra":
        body["answers"]["other"] = body["answers"]["q0"]
    elif kind == "unmapped_caller_id":
        body["answers"]["meiva-guide:mcp"] = body["answers"].pop("q0")
    elif kind == "out_of_range":
        body["answers"]["q0"]["noul"] = 2
    else:
        body["answers"]["q0"]["type"] = "choice"
    try:
        result = await binding(lambda _: httpx.Response(200, json=body)).decide("text", QUESTIONS)
    except DecisionResponseError as exc:
        assert exc.input_tokens == 225
        assert expected_fragment in str(exc)
    else:
        with pytest.raises(DecisionResponseError, match=re.escape(expected_fragment)):
            validate_decisions(QUESTIONS, result.decisions)


@pytest.mark.parametrize("blank_id", ["", "   "])
async def test_blank_question_id_fails_before_request(blank_id: str) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network was called")

    with pytest.raises(ValueError, match="non-empty"):
        await binding(unexpected).decide("text", {blank_id: QUESTIONS["blue"]})


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


async def test_recall_skills_scores_colon_and_long_subskills_over_gemini_server(
    model_catalog: ModelCatalog,
    gemini_server: GeminiServer,
    loguru_records: LogRecords,
) -> None:
    long_subskill_name = "reproducible-measurement-and-ablation-control-playbook"
    skills = (
        Skill(
            name="mcp",
            description="Add, inspect, or remove MCP servers.",
            path="/skills/meiva-guide/mcp/SKILL.md",
            namespace=("meiva-guide",),
        ),
        Skill(
            name="rss",
            description="Fetch RSS and Atom feeds.",
            path="/skills/rss-pack/rss/SKILL.md",
            namespace=("rss-pack",),
        ),
        Skill(
            name=long_subskill_name,
            description="Design reproducible ablation measurements.",
            path=f"/skills/meiva-serious-research/practices/{long_subskill_name}/SKILL.md",
            namespace=("meiva-serious-research", "practices"),
        ),
    )
    candidates = recall_candidates(SkillView(inject=("*",), expand=(), disabled=(), skills=skills))
    long_identifier = f"meiva-serious-research:practices:{long_subskill_name}"
    assert len(long_identifier) > 64
    assert [c.identifier for c in candidates] == [
        "meiva-guide:mcp",
        "rss-pack:rss",
        long_identifier,
    ]

    gemini_server.answer_decisions(
        {
            "q0": {"type": "noul", "noul": 0.91, "confidence": 0.91},
            "q1": {"type": "noul", "noul": 0.05, "confidence": 0.95},
            "q2": {"type": "noul", "noul": 0.84, "confidence": 0.84},
        },
        input_tokens=190,
    )
    provider = model_catalog.decision_provider_of(MODEL)
    assert provider is not None
    original_build = provider.build

    def build_over_server(ctx: DecisionBuildContext) -> DecisionBinding:
        return original_build(replace(ctx, transport=gemini_server.transport()))

    catalog = replace(
        model_catalog,
        decision_providers={
            **model_catalog.decision_providers,
            provider.prefix: replace(provider, build=build_over_server),
        },
    )

    recalled = await recall_skills(
        [
            inbound_message(
                content="Configure an MCP server and verify the ablation measurement setup.",
                source="user",
                inbound_id=1,
                body_start=0,
                times=InboundTimes(
                    arrived_at=None, picked_up_at=datetime(2026, 11, 16, tzinfo=UTC)
                ),
            )
        ],
        candidates,
        agent=AgentSlices.resolve({}, default_reader=defaults()),
        catalog=catalog,
        keys=inert_keys(catalog),
        agent_id=7683,
        created_at=datetime(2026, 11, 16, tzinfo=UTC),
        inputs=TimestampPolicy.off(),
    )

    assert loguru_records.messages("WARNING") == []
    assert recalled is not None
    assert recalled.identifiers == {"meiva-guide:mcp", long_identifier}
    assert "`meiva.skills.meiva_guide.mcp`" in recalled.note.text
    assert (
        "`meiva.skills.meiva_serious_research.practices."
        "reproducible_measurement_and_ablation_control_playbook`" in recalled.note.text
    )
    assert "rss_pack" not in recalled.note.text
    [received] = gemini_server.received
    assert received.path == "/v1beta/decisions"
    wire_questions = received.body["questions"]
    assert is_object_dict(wire_questions)
    assert list(wire_questions) == ["q0", "q1", "q2"]
