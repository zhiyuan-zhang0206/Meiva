# meiva:note
# Default Gemini decision recall must translate colon-delimited and >64-character skill identifiers
# into wire-legal question keys instead of triggering HTTP 400 skips at the before-LLM boundary.
# meiva:link agent/graph/docs/skill-recall.md
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime

import httpx

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
from base.lm.provider_api import DecisionBinding, DecisionBuildContext
from base.log.tests.log_records import LogRecords
from base.packages.skills.tree import Skill
from base.packages.tests.model_catalog import inert_keys

_GEMINI_QUESTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def test_default_provider_recalls_colon_and_over_64_char_subskills_without_http_400(
    model_catalog: ModelCatalog,
    loguru_records: LogRecords,
) -> None:
    long_leaf = "reproducible-measurement-and-ablation-control-playbook"
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
            name=long_leaf,
            description="Design reproducible ablation measurements.",
            path=f"/skills/meiva-serious-research/practices/{long_leaf}/SKILL.md",
            namespace=("meiva-serious-research", "practices"),
        ),
    )
    candidates = recall_candidates(SkillView(inject=("*",), expand=(), disabled=(), skills=skills))
    long_identifier = f"meiva-serious-research:practices:{long_leaf}"
    assert len(long_identifier) > 64

    provider = model_catalog.decision_provider_of("gemini-decision-1")
    assert provider is not None
    original_build = provider.build
    wire_keys_seen: list[list[str]] = []

    def strict_gemini_decisions(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
        body = object_of(json.loads, request.content)
        assert is_object_dict(body)
        questions = body.get("questions")
        assert is_object_dict(questions) and questions
        for qid in questions:
            if not _GEMINI_QUESTION_ID.fullmatch(qid):
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "message": (
                                f"* CreateDecisionRequest.questions[{qid}]: "
                                "question IDs must be 1-64 characters matching [A-Za-z0-9_-]+"
                            ),
                            "status": "INVALID_ARGUMENT",
                        }
                    },
                )
        keys = list(questions)
        wire_keys_seen.append(keys)
        answers: dict[str, dict[str, object]] = {}
        for qid, payload in questions.items():
            assert is_object_dict(payload)
            instructions = payload.get("instructions")
            assert isinstance(instructions, str)
            relevant = "meiva-guide:mcp" in instructions or long_identifier in instructions
            prob = 0.92 if relevant else 0.04
            answers[qid] = {
                "type": "noul",
                "noul": prob,
                "confidence": max(prob, 1.0 - prob),
            }
        return httpx.Response(
            200,
            json={
                "model": "gemini-decision-1",
                "answers": answers,
                "usage": {"total_input_tokens": 175, "total_output_tokens": 0},
            },
        )

    def build(ctx: DecisionBuildContext) -> DecisionBinding:
        return original_build(replace(ctx, transport=httpx.MockTransport(strict_gemini_decisions)))

    catalog = replace(
        model_catalog,
        decision_providers={
            **model_catalog.decision_providers,
            provider.prefix: replace(provider, build=build),
        },
    )

    result = await recall_skills(
        [
            inbound_message(
                content="Add an MCP server and check the ablation measurement rubric.",
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

    assert wire_keys_seen == [["q0", "q1", "q2"]]
    assert loguru_records.messages("WARNING") == []
    assert result is not None
    assert result.identifiers == {"meiva-guide:mcp", long_identifier}
    usage_rows = loguru_records.extras(event="llm_usage")
    assert len(usage_rows) == 1
    assert usage_rows[0]["model"] == "gemini-decision-1"
    assert usage_rows[0]["in_total"] == 175
