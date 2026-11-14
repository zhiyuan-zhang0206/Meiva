# meiva:note
# The real default provider's question cap must degrade at recall's existing safe-skip boundary.
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from agent.graph.prompt.capabilities import RecallCandidate
from agent.graph.recall.skill_recall import recall_skills
from agent.messages import InboundTimes, inbound_message
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.provider_api import DecisionBinding, DecisionBuildContext
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys
from base.paths import meiva_home


@pytest.mark.parametrize("count", [0, 1001])
async def test_default_provider_question_capacity_safely_skips_recall_without_http(
    count: int,
) -> None:
    catalog = build_model_catalog(home=meiva_home())
    provider = catalog.decision_provider_of("gemini-decision-1")
    assert provider is not None
    original_build = provider.build
    builds: list[str] = []
    requests: list[httpx.Request] = []

    def unexpected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("capacity rejection reached HTTP")

    def build(ctx: DecisionBuildContext) -> DecisionBinding:
        builds.append(ctx.model)
        return original_build(replace(ctx, transport=httpx.MockTransport(unexpected)))

    catalog = replace(
        catalog,
        decision_providers={
            **catalog.decision_providers,
            provider.prefix: replace(provider, build=build),
        },
    )
    result = await recall_skills(
        [
            inbound_message(
                content="Find the skill for this synthetic request.",
                source="user",
                inbound_id=1,
                body_start=0,
                times=InboundTimes(
                    arrived_at=None, picked_up_at=datetime(2026, 11, 12, tzinfo=UTC)
                ),
            )
        ],
        [RecallCandidate(f"skill{n}", "Synthetic skill", f"skill{n}") for n in range(count)],
        agent=AgentSlices.resolve({}, default_reader=defaults()),
        catalog=catalog,
        keys=inert_keys(catalog),
        agent_id=1,
        created_at=datetime(2026, 11, 12, tzinfo=UTC),
        inputs=TimestampPolicy.off(),
    )
    assert result is None
    assert builds == (["gemini-decision-1"] if count else [])
    assert requests == []
