# meiva:note
# What happens after the timeline writer commits a read, on the recorded session with two sub-agents (CLI 2.1.322):
# every reliable model message's spend is logged once, the main agent's as `usage_kind="agent"` and each
# sub-agent's as `usage_kind="agent_subagent"` with the call that started it and the sub-agent's own model, the
# main figure excludes the sub-agents' (main 858 output tokens, sub-agents 252 and 217), an interrupted message is
# not logged, a read twice logs nothing the second time, and a row the parser could not use is reported.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from agent.graph.claim.tests.harness_support import FakeEngine, runtime
from agent.harness_timeline.plan import ReplyPolicy, WritePlan, plan_write
from agent.harness_timeline.writer import Committed
from base.lm.harness import Usage
from base.lm.harness.claude.transcript.tests.support import entries_of, project, sent
from base.lm.harness.session.transcript.drafts import AnomalyReport, MessageUsage
from base.lm.harness.session.transcript.entries import AnomalyKind
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.agent_runner.agent_host.transcript.effects import (
    MAIN_USAGE_KIND,
    SUBAGENT_USAGE_KIND,
    TimelineEffects,
)

SUBS = "611dfcac-5809-5de8-b8c6-2e185bf7d5be"
POLICY = ReplyPolicy(explicit_visibility=False, delivery_mode=None)


def _plan() -> WritePlan:
    entries = [
        *entries_of("subagents"),
        *entries_of("subagents", "af6798fb938fb227f"),
        *entries_of("subagents", "a0044dfe6d0ac4533"),
    ]
    _, projection = project(entries, [sent(SUBS, "k", "t")])
    return plan_write(
        projection,
        sent_items={},
        inbound_ids={},
        non_human_turns=frozenset(),
        policy=POLICY,
        render_tool=_name,
    )


async def test_main_and_sub_agent_spend_are_logged_separately_by_call_and_model(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7)
    effects.bind(ctx)
    plan = _plan()
    await effects.after_commit(Committed(plan, ()))

    events = [record["extra"] for record in loguru_records.events("llm_usage")]
    main = [e for e in events if e["usage_kind"] == MAIN_USAGE_KIND]
    subs = [e for e in events if e["usage_kind"] == SUBAGENT_USAGE_KIND]
    assert len(main) + len(subs) == len(plan.usage) == len(events)
    assert sum(e["out_total"] for e in main) == 858 + 182, "turn 1 is 858, turn 2 is 182"
    by_call: Counter[str] = Counter()
    for event in subs:
        by_call[event["parent_call_id"]] += event["out_total"]
    assert sorted(by_call.values()) == [217, 252]
    assert {e["model"] for e in subs} == {"claude-haiku-5-5"}
    assert all("parent_call_id" not in e for e in main)
    assert {e["source"] for e in events} == {"claude_code"} and {e["calls"] for e in events} == {1}


async def test_an_interrupted_message_and_an_all_zero_message_are_not_logged(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7)
    effects.bind(ctx)
    plan = WritePlan(
        usage=(
            MessageUsage(
                "main", "m1", "t", None, Usage(input_tokens=5, output_tokens=2), "x", True
            ),
            MessageUsage("main", "m2", "t", None, Usage(), "x", True),
        )
    )
    await effects.after_commit(Committed(plan, ()))
    assert len(loguru_records.events("llm_usage")) == 1


async def test_what_the_parser_could_not_use_is_reported(loguru_records: LogRecords) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7)
    effects.bind(ctx)
    plan = WritePlan(
        anomalies=(AnomalyReport(AnomalyKind.UNKNOWN_TYPE, "weird", "k"),),
        unmatched_inputs=("claude:u-1",),
    )
    await effects.after_commit(Committed(plan, ()))
    assert [r["extra"]["detail"] for r in loguru_records.events("harness_timeline_anomaly")] == [
        "weird"
    ]
    assert len(loguru_records.events("harness_timeline_unmatched_input")) == 1


def _name(name: str, tool_input: Mapping[str, object]) -> str:
    del tool_input
    return name
