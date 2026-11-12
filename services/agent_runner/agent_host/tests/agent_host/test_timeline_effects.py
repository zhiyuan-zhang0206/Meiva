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
from agent.harness_timeline.plan import WritePlan, plan_write
from agent.harness_timeline.writer import Committed
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy
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


class Published:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    def emit(self, payload: str) -> None:
        self.payloads.append(payload)


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
    effects = TimelineEffects(7, Published())
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
    assert {e["source"] for e in events} == {"codex"} and {e["calls"] for e in events} == {1}


async def test_an_interrupted_message_and_an_all_zero_message_are_not_logged(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7, Published())
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
    effects = TimelineEffects(7, Published())
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


class _Failing:
    def __init__(self) -> None:
        self.fail = True
        self.captured: list[str] = []

    async def capture(self, ctx: object, agent_id: int, items: list[TimelineItem]) -> None:
        del ctx, agent_id
        if self.fail:
            raise OSError("the delivery store is down")
        self.captured.extend(item.item_id for item in items)


def _reply(item_id: str) -> TimelineItem:
    return TimelineItem(item_id=item_id, kind="agent_chat", payload="hi", user_facing=True)


async def test_a_failed_capture_costs_neither_the_usage_nor_the_publish_and_is_tried_again(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    published = Published()
    store = _Failing()
    effects = TimelineEffects(7, published, capture=store.capture)
    effects.bind(ctx)
    usage = MessageUsage("main", "m1", "t", None, Usage(input_tokens=5, output_tokens=2), "x", True)
    first = _reply("h1.0")
    await effects.after_commit(Committed(WritePlan(usage=(usage,)), (first,)))

    assert len(published.payloads) >= 1, "the live view was published although the capture failed"
    assert len(loguru_records.events("llm_usage")) == 1, (
        "the spend was logged although the capture failed"
    )
    failures = loguru_records.events("harness_timeline_effect_failed")
    assert [r["extra"]["effect"] for r in failures] == ["capture"]

    store.fail = False
    await effects.after_commit(Committed(WritePlan(usage=(usage,)), (_reply("h2.0"),)))
    assert store.captured == ["h1.0", "h2.0"], "what failed is offered again with the next commit"
    assert len(loguru_records.events("llm_usage")) == 1, (
        "a spend is logged once however often it is planned"
    )


async def test_the_first_unmatched_inputs_of_a_session_are_each_reported_and_then_one_in_a_hundred(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7, Published())
    effects.bind(ctx)
    for n in range(250):
        await effects.after_commit(Committed(WritePlan(unmatched_inputs=(f"claude:u-{n}",)), ()))
    seen = [
        r["extra"]["seen_in_this_session"]
        for r in loguru_records.events("harness_timeline_unmatched_input")
    ]
    assert seen == [*range(1, 21), 101, 201]


async def test_replies_committed_before_a_crash_are_offered_to_the_capture_again_at_start() -> None:
    ctx = runtime(FakeEngine()).context
    store = _Failing()
    store.fail = False
    effects = TimelineEffects(7, Published(), capture=store.capture)
    effects.bind(ctx)

    async def stored(rows: int) -> list[TimelineItem]:
        assert rows > 0
        return [_reply("h4.0"), _reply("h7.0")]

    await effects.recover(stored)

    assert store.captured == ["h4.0", "h7.0"]


async def test_a_message_cut_short_is_counted_not_priced_and_a_model_without_a_price_is_warned_once(
    loguru_records: LogRecords,
) -> None:
    ctx = runtime(FakeEngine()).context
    effects = TimelineEffects(7, Published())
    effects.bind(ctx)
    cut = MessageUsage(
        "main", "m-cut", "t", None, Usage(input_tokens=1, output_tokens=1), "x", False
    )
    unpriced = [
        MessageUsage(
            "main",
            f"m{i}",
            "t",
            None,
            Usage(input_tokens=5, output_tokens=2),
            "no-such-model",
            True,
        )
        for i in range(3)
    ]
    plan = WritePlan(usage=tuple(unpriced), unreliable_usage=(cut, cut))
    await effects.after_commit(Committed(plan, ()))
    assert len(loguru_records.events("harness_timeline_usage_unreliable")) == 1, (
        "one event per message"
    )
    assert len(loguru_records.events("llm_usage")) == 3, "the unpriced messages are all written"
    warned = [m for m in loguru_records.messages("WARNING") if "is unpriced" in m]
    assert len(warned) == 1, "and warned about once"
