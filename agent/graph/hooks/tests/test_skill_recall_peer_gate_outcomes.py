# meiva:note
# What the peer gate records for each decision, and the request timeout both skill-recall calls
# carry by default, over the real `decide()` with a scripted binding behind the default provider.
# meiva:link agent/graph/docs/skill-recall.md
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent.graph.prompt.capabilities import RecallCandidate
from agent.graph.recall.skill_recall import peer_gate_passes, recall_skills
from agent.messages import InboundTimes, inbound_message
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.provider_api import (
    Decision,
    DecisionBuildContext,
    DecisionRequestError,
    DecisionResult,
    Question,
)
from base.log.tests.log_records import LogRecords
from base.packages.tests.model_catalog import inert_keys

_MODEL = "gemini-decision-1"
_AT = datetime(2026, 11, 18, tzinfo=UTC)


# meiva:note
# A decision binding that answers every question with one scripted probability of "true", or
# raises `error`, and records the request timeout each build was given.
class _Binding:
    def __init__(self, probability: float = 0.0, *, error: Exception | None = None) -> None:
        self.probability = probability
        self.error = error
        self.timeouts: list[float | None] = []

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        del state
        if self.error is not None:
            raise self.error
        p = self.probability
        return DecisionResult(
            {
                qid: Decision(
                    top="true" if p >= 0.5 else "false", probabilities={"true": p, "false": 1 - p}
                )
                for qid in questions
            },
            input_tokens=10,
        )


def _catalog(model_catalog: ModelCatalog, binding: _Binding) -> ModelCatalog:
    provider = model_catalog.decision_provider_of(_MODEL)
    assert provider is not None

    def build(ctx: DecisionBuildContext) -> _Binding:
        binding.timeouts.append(ctx.timeout)
        return binding

    return replace(
        model_catalog,
        decision_providers={
            **model_catalog.decision_providers,
            provider.prefix: replace(provider, build=build),
        },
    )


# meiva:note
# A clock that moves 250ms forward at every reading, so an elapsed time measured on it is positive.
def _stepping_clock() -> Callable[[], datetime]:
    readings = iter(_AT + timedelta(milliseconds=250 * n) for n in range(1000))
    return lambda: next(readings)


async def _gate(model_catalog: ModelCatalog, binding: _Binding) -> bool:
    catalog = _catalog(model_catalog, binding)
    return await peer_gate_passes(
        "please review PR 1234 against the docs",
        agent=AgentSlices.resolve({}, default_reader=defaults()),
        catalog=catalog,
        keys=inert_keys(catalog),
        agent_id=7798,
        now=_stepping_clock(),
    )


def _gate_event(loguru_records: LogRecords) -> dict[str, object]:
    (event,) = [
        e for e in loguru_records.extras(event="skill_recall") if e.get("stage") == "peer_gate"
    ]
    return event


@pytest.mark.parametrize(
    ("work", "outcome", "passed"), [(0.95, "passed", True), (0.05, "skipped", False)]
)
async def test_a_decided_gate_records_its_outcome_latency_and_work(
    model_catalog: ModelCatalog, loguru_records: LogRecords, work: float, outcome: str, passed: bool
) -> None:
    assert await _gate(model_catalog, _Binding(work)) is passed

    event = _gate_event(loguru_records)
    assert event["outcome"] == outcome
    assert event["work"] == work
    latency = event["latency_ms"]
    assert isinstance(latency, float) and latency > 0
    assert "reason" not in event
    assert loguru_records.messages("WARNING") == []


async def test_a_failed_gate_records_the_innermost_cause_at_warning(
    model_catalog: ModelCatalog, loguru_records: LogRecords
) -> None:
    error = DecisionRequestError(f"{_MODEL}: request failed: ReadTimeout")
    error.__cause__ = httpx.ReadTimeout("read timed out")

    assert await _gate(model_catalog, _Binding(error=error)) is False

    event = _gate_event(loguru_records)
    assert event["outcome"] == "failed"
    assert event["reason"] == "ReadTimeout"
    latency = event["latency_ms"]
    assert isinstance(latency, float) and latency > 0
    assert "work" not in event
    assert loguru_records.extras(event="skill_recall", level="WARNING") == [event]


async def test_a_failure_without_a_cause_records_its_own_class(
    model_catalog: ModelCatalog, loguru_records: LogRecords
) -> None:
    assert await _gate(model_catalog, _Binding(error=DecisionRequestError("HTTP 503"))) is False

    assert _gate_event(loguru_records)["reason"] == "DecisionRequestError"


async def test_the_gate_and_the_recall_pass_send_the_six_second_default_timeout(
    model_catalog: ModelCatalog,
) -> None:
    gate = _Binding(0.95)
    _ = await _gate(model_catalog, gate)

    scorer = _Binding(0.9)
    catalog = _catalog(model_catalog, scorer)
    recalled = await recall_skills(
        [
            inbound_message(
                content="Draft the release notes.",
                source="user",
                inbound_id=1,
                body_start=0,
                times=InboundTimes(arrived_at=None, picked_up_at=_AT),
            )
        ],
        [RecallCandidate("alpha", "Alpha skill", "alpha")],
        agent=AgentSlices.resolve({}, default_reader=defaults()),
        catalog=catalog,
        keys=inert_keys(catalog),
        agent_id=7798,
        created_at=_AT,
        inputs=TimestampPolicy.off(),
    )

    assert recalled is not None
    assert gate.timeouts == [6.0]
    assert scorer.timeouts == [6.0]
