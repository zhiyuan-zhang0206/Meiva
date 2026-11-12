from __future__ import annotations

import re

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.antigravity.transcript.parse import (
    OpenCall,
    cancel_entries,
    parse_rows,
)
from base.lm.harness.antigravity.transcript.tests.support import (
    Scenario,
    keys_of,
    kinds,
    load,
    parse,
    parse_child,
    sent,
)
from base.lm.harness.events import HarnessCause, TurnOrigin
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    MatchBasis,
    Projection,
    SentInput,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    HumanInput,
    Interrupt,
    Notice,
    Reasoning,
    SubagentLink,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from base.lm.harness.session.transcript.projection import Projector

# meiva:note
# Golden tests of the Antigravity parser against the shared projection. Every fixture is CONSTRUCTED (see
# `CONSTRUCTED` in `support.py`): the rows are reconstructed from trajectory captures because raw
# `transcript_full.jsonl` lines were not recorded, and the sub-agent's own rows (`child.jsonl`) are
# synthesized from log summaries with invented text and token counts. Passing here proves the parser
# and the shared vocabulary fit the documented row shape, not that the real file has it; a re-record of
# raw `transcript_full.jsonl` is required before the Antigravity cutover. Every test goes fixture ->
# parser -> `Projector` and asserts what the projection shows, not what the parser holds.
# meiva:link base/lm/harness/antigravity/transcript/tests/fixtures/cancel_then_subagent/source.json


def project(entries: tuple[AnyEntry, ...], *registered: SentInput) -> tuple[Projector, Projection]:
    projector = Projector()
    for item in registered:
        projector.register(item)
    return projector, projector.feed(entries)


def drafts_of(projection: Projection, kind: DraftKind) -> list[TimelineDraft]:
    return [draft for draft in projection.drafts if draft.kind is kind]


def only[T](items: list[T]) -> T:
    assert len(items) == 1, items
    return items[0]


def step_text(scenario: Scenario, step: int) -> str:
    for row in scenario.rows:
        if is_object_dict(row) and row["step_index"] == step:
            return str(row["content"])
    raise AssertionError(step)


def test_basic_turn_pairs_the_call_with_its_output_and_answers() -> None:
    scenario = load("basic_turn")
    conv = scenario.conversation_id
    _, projection = project(parse(scenario).entries)
    assert kinds(projection.drafts) == ["inbound_chat", "agent_code", "code_output", "agent_chat"]
    call = drafts_of(projection, DraftKind.AGENT_CODE)[0]
    output = drafts_of(projection, DraftKind.CODE_OUTPUT)[0]
    assert call.tool_name == "run_command"
    assert call.tool_input == {
        "CommandLine": "echo hello-tx; sleep 3; echo done-tx",
        "WaitMsBeforeAsync": 5000,
        "toolSummary": "Run echo and sleep commands",
    }
    assert call.tool_use_id == output.tool_use_id == f"agy:{conv}:1:0"
    assert output.tool_status is ToolStatus.OK
    assert output.payload.startswith("Command exited with code 0.")
    assert "Created At" not in output.payload
    assert "hello-tx" in output.payload and "done-tx" in output.payload
    assert output.exec_ms is None
    assert [draft.order.seq for draft in projection.drafts] == [0, 1, 2, 3]
    assert all(
        left.order <= right.order
        for left, right in zip(projection.drafts, projection.drafts[1:], strict=False)
    )
    assert not projection.anomalies


def test_keys_are_stable_and_named_by_conversation_and_step() -> None:
    for name in ("basic_turn", "timer_and_compaction", "steer_background"):
        scenario = load(name)
        pattern = re.compile(rf"^agy:{scenario.conversation_id}:\d+(:[a-z]+(:\d+)?)?$")
        entries = parse(scenario).entries
        assert all(pattern.match(key) for key in keys_of(entries)), keys_of(entries)
        assert len(set(keys_of(entries))) == len(entries)
        assert keys_of(parse(scenario).entries) == keys_of(entries)


def test_thinking_summary_becomes_a_reasoning_draft_before_the_call() -> None:
    scenario = load("thinking_turn")
    entries = parse(scenario).entries
    reasoning = only([entry for entry in entries if isinstance(entry, Reasoning)])
    assert reasoning.text.startswith("The calculation of 17 multiplied by 23 yields 391.")
    assert reasoning.text == reasoning.text.strip()
    _, projection = project(entries)
    assert kinds(projection.drafts) == [
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_chat",
    ]
    thinking, call = projection.drafts[1], projection.drafts[2]
    assert thinking.order.seq == call.order.seq == 1
    assert thinking.order < call.order


def test_ordinary_input_has_no_id_and_is_recognised_by_its_text() -> None:
    scenario = load("basic_turn")
    human = only([entry for entry in parse(scenario).entries if isinstance(entry, HumanInput)])
    assert human.wire_id is None
    assert human.text == (
        "Run the shell command: echo hello-tx; sleep 3; echo done-tx . Then reply in one short sentence."
    )
    message = sent("in-1", "our-wire-id", human.text)
    _, projection = project(parse(scenario).entries, message)
    delivery = only(list(projection.deliveries))
    assert delivery.input_key == "in-1"
    assert delivery.basis is MatchBasis.TEXT
    assert delivery.joined_running_turn is False
    shown = only(drafts_of(projection, DraftKind.INBOUND_CHAT))
    assert shown.part_ref == "in-1-part" and shown.payload == human.text
    assert not projection.unmatched_inputs


def test_rpc_injected_input_is_recognised_by_its_wire_id() -> None:
    scenario = load("idle_injection")
    entries = parse(scenario).entries
    humans = [entry for entry in entries if isinstance(entry, HumanInput)]
    assert [human.wire_id for human in humans] == [None, "tx-idle-1", None]
    registered = (
        sent("in-1", "w-1", humans[0].text),
        sent("in-2", "tx-idle-1", humans[1].text),
        sent("in-3", "w-3", humans[2].text),
    )
    _, projection = project(entries, *registered)
    assert {d.input_key: d.basis for d in projection.deliveries} == {
        "in-1": MatchBasis.TEXT,
        "in-2": MatchBasis.WIRE_ID,
        "in-3": MatchBasis.TEXT,
    }
    assert not projection.unmatched_inputs


def test_without_the_trajectory_side_the_injected_input_is_still_recognised_by_text() -> None:
    scenario = load("idle_injection")
    entries = parse(scenario, side=False).entries
    humans = [entry for entry in entries if isinstance(entry, HumanInput)]
    assert [human.wire_id for human in humans] == [None, None, None]
    _, projection = project(entries, sent("in-2", "tx-idle-1", humans[1].text))
    delivery = only(list(projection.deliveries))
    assert (delivery.input_key, delivery.basis) == ("in-2", MatchBasis.TEXT)
    assert len(projection.unmatched_inputs) == 2


def test_foreground_cancel_leaves_a_hole_and_the_call_unanswered() -> None:
    scenario = load("cancel_then_subagent")
    conv = scenario.conversation_id
    first = parse_rows(scenario.rows[:2], conversation_id=conv)
    assert [call.step for call in first.state.open_calls] == [2]
    projector, shown = project(first.entries)
    assert kinds(shown.drafts) == ["inbound_chat", "agent_code"]
    after_hole = parse_rows(scenario.rows[2:3], conversation_id=conv, state=first.state)
    assert [entry.key for entry in after_hole.entries] == [f"agy:{conv}:3"]
    again = projector.feed(after_hole.entries)
    assert kinds(again.drafts) == ["inbound_chat"]
    assert not drafts_of(again, DraftKind.CODE_OUTPUT)
    assert not again.turns_closed
    assert after_hole.state.open_calls == (OpenCall(2, f"agy:{conv}:1:0", "run_command", None),)
    assert projector.unresolved_results() == ()


SIGNAL_MS = 1794474081500


# meiva:note
# CONSTRUCTED rows (s6 parent); the cancel signal itself is a transport event the record never holds.
def test_cancel_signal_yields_an_interrupt_that_sorts_before_the_next_row() -> None:
    scenario = load("cancel_then_subagent")
    conv = scenario.conversation_id
    first = parse_rows(scenario.rows[:2], conversation_id=conv)
    cancelled = cancel_entries(first.state, conversation_id=conv, clock_ms=SIGNAL_MS)
    interrupt = only([entry for entry in cancelled.entries if isinstance(entry, Interrupt)])
    assert interrupt.key == f"agy:{conv}:cancel:1"
    assert interrupt.order.seq == 1 and interrupt.order > first.entries[-1].order
    assert cancelled.state.open_calls == ()
    later = parse_rows(scenario.rows[2:3], conversation_id=conv, state=cancelled.state)
    assert later.entries[0].order > interrupt.order
    again = cancel_entries(cancelled.state, conversation_id=conv, clock_ms=SIGNAL_MS)
    assert [entry for entry in again.entries if isinstance(entry, ToolResult)] == []


def test_cancel_signal_closes_the_turn_and_aborts_the_unanswered_call() -> None:
    scenario = load("cancel_then_subagent")
    conv = scenario.conversation_id
    first = parse_rows(scenario.rows[:2], conversation_id=conv)
    cancelled = cancel_entries(first.state, conversation_id=conv, clock_ms=SIGNAL_MS)
    projector, shown = project(first.entries + cancelled.entries)
    aborted = only(drafts_of(shown, DraftKind.CODE_OUTPUT))
    assert aborted.tool_status is ToolStatus.ABORTED
    assert aborted.tool_use_id == f"agy:{conv}:1:0"
    assert aborted.payload == "Tool failed"
    assert any(
        d.payload == "status:Turn interrupted" for d in drafts_of(shown, DraftKind.SYSTEM_MARKER)
    )
    assert [closed.reason for closed in shown.turns_closed] == ["interrupted"]
    later = parse_rows(scenario.rows[2:3], conversation_id=conv, state=cancelled.state)
    nxt = projector.feed(later.entries)
    assert len(nxt.turns_observed) == 1 and nxt.turns_observed[0].origin is TurnOrigin.INPUT


def test_background_command_is_a_background_result_and_its_end_is_a_later_notice() -> None:
    scenario = load("steer_background")
    conv = scenario.conversation_id
    entries = parse(scenario).entries
    running = only([e for e in entries if isinstance(e, ToolResult)])
    assert running.status is ToolStatus.BACKGROUND
    assert running.call_id == f"agy:{conv}:8:0" and running.key == f"agy:{conv}:9"
    assert running.content.startswith("Tool is running as a background task with task id:")
    assert "YOU MUST" not in running.content
    notice = only([e for e in entries if isinstance(e, Notice)])
    assert notice.key == f"agy:{conv}:12" and notice.cause is HarnessCause.BACKGROUND_TASK
    assert notice.text.startswith(f'Task id "{conv}/task-9" finished with result:')
    assert "Command output:\nok" in notice.text
    _, projection = project(entries)
    output = only(drafts_of(projection, DraftKind.CODE_OUTPUT))
    assert output.tool_status is ToolStatus.BACKGROUND
    marker = only(drafts_of(projection, DraftKind.SYSTEM_MARKER))
    assert marker.source == "background_task" and marker.order.seq == 12


def test_background_command_that_is_cancelled_stays_a_background_result_forever() -> None:
    scenario = load("idle_injection")
    entries = parse(scenario).entries
    results = [entry for entry in entries if isinstance(entry, ToolResult)]
    assert [(r.key.rsplit(":", 1)[1], r.status) for r in results] == [("6", ToolStatus.BACKGROUND)]
    state = parse(scenario).state
    assert state.open_calls == ()
    assert (
        cancel_entries(state, conversation_id=scenario.conversation_id, clock_ms=1).entries[1:]
        == ()
    )


# meiva:note
# CONSTRUCTED rows (s3); the side input is read from the trajectory.
def test_steer_entry_is_written_after_the_step_before_it_and_joins_the_turn() -> None:
    scenario = load("steer_background")
    conv = scenario.conversation_id
    assert "<USER_REQUEST>" in step_text(scenario, 10)
    head = parse_rows(scenario.rows[:4], conversation_id=conv, side=scenario.side)
    tail = parse_rows(scenario.rows[4:], conversation_id=conv, state=head.state, side=scenario.side)
    steer_entry = only([e for e in tail.entries if isinstance(e, HumanInput)])
    assert steer_entry.wire_id == "tx-steer-1" and steer_entry.joined_running_turn is True
    assert (
        steer_entry.text
        == "Additional instruction: in your final reply also include the word BRAVO."
    )
    assert steer_entry.order.seq == 10 and steer_entry.order > head.entries[-1].order


def test_steer_is_delivered_by_wire_id_into_the_turn_that_was_running() -> None:
    scenario = load("steer_background")
    conv = scenario.conversation_id
    head = parse_rows(scenario.rows[:4], conversation_id=conv, side=scenario.side)
    tail = parse_rows(scenario.rows[4:], conversation_id=conv, state=head.state, side=scenario.side)
    humans = [e for e in parse(scenario).entries if isinstance(e, HumanInput)]
    projector = Projector()
    projector.register(sent("in-1", "w-1", humans[0].text))
    projector.register(sent("in-2", "tx-steer-1", humans[1].text))
    before = projector.feed(head.entries)
    assert [d.input_key for d in before.deliveries] == ["in-1"]
    assert before.deliveries[0].joined_running_turn is False
    assert not any("BRAVO" in draft.payload for draft in before.drafts)
    after = projector.feed(tail.entries)
    delivery = only(list(after.deliveries))
    assert (delivery.input_key, delivery.basis) == ("in-2", MatchBasis.WIRE_ID)
    assert delivery.joined_running_turn is True
    assert delivery.turn == before.deliveries[0].turn
    assert not after.unmatched_inputs


def test_phantom_turn_without_a_stream_signal_is_a_normal_turn() -> None:
    scenario = load("idle_injection")
    entries = parse(scenario).entries
    humans = [entry for entry in entries if isinstance(entry, HumanInput)]
    projector, projection = project(entries, sent("in-2", "tx-idle-1", humans[1].text))
    phantom = next(d for d in projection.deliveries if d.input_key == "in-2")
    assert phantom.basis is MatchBasis.WIRE_ID and phantom.joined_running_turn is False
    answer = next(d for d in drafts_of(projection, DraftKind.AGENT_CHAT) if d.payload == "PHANTOM")
    assert answer.turn == phantom.turn
    origins = [observed.origin for observed in projection.turns_observed]
    assert origins == [TurnOrigin.INPUT, TurnOrigin.INPUT, TurnOrigin.INPUT]
    usage = projector.usage_by_turn()[phantom.turn or ""]
    assert (usage.usage.input_tokens, usage.usage.output_tokens) == (11164, 3)


# meiva:note
# CONSTRUCTED rows: the parent is reconstructed (s6), the child record is synthesized.
def _check_nesting(scenario: Scenario, *, child_first: bool) -> frozenset[str]:
    child = scenario.child_id
    parent_entries, child_entries = parse(scenario).entries, parse_child(scenario).entries
    link = only([e for e in parent_entries if isinstance(e, SubagentLink)])
    first, second = (
        (child_entries, parent_entries) if child_first else (parent_entries, child_entries)
    )
    projector, projection = project(first)
    projection2 = projector.feed(second)
    drafts = projection.drafts + projection2.drafts
    nested = [d for d in drafts if d.record == child]
    assert nested and all(d.parent_tool_use_id == link.call_id for d in nested)
    assert projector.unlinked_records() == ()
    assert kinds(tuple(nested)) == [
        "subagent_text",
        "agent_code",
        "code_output",
        "subagent_text",
        "subagent_text",
        "agent_code",
        "code_output",
        "subagent_text",
    ]
    assert nested[2].tool_status is ToolStatus.BACKGROUND
    assert all(d.parent_tool_use_id is None for d in drafts if d.record == MAIN_RECORD)
    return frozenset(d.key for d in drafts)


def test_subagent_link_names_the_call_and_the_child_record() -> None:
    scenario = load("cancel_then_subagent")
    child_entries = parse_child(scenario).entries
    link = only([e for e in parse(scenario).entries if isinstance(e, SubagentLink)])
    assert link.call_id == f"agy:{scenario.conversation_id}:4:0"
    assert (link.child_record, link.label) == (scenario.child_id, "Shell Command Runner")
    assert all(entry.record == scenario.child_id for entry in child_entries)


def test_subagent_records_nest_when_the_parent_arrives_first() -> None:
    assert _check_nesting(load("cancel_then_subagent"), child_first=False)


def test_subagent_records_nest_when_the_child_arrives_first() -> None:
    assert _check_nesting(load("cancel_then_subagent"), child_first=True)


def test_subagent_drafts_are_the_same_set_in_either_arrival_order() -> None:
    scenario = load("cancel_then_subagent")
    assert _check_nesting(scenario, child_first=False) == _check_nesting(scenario, child_first=True)


def test_subagent_usage_is_kept_apart_from_the_parent_turn() -> None:
    scenario = load("cancel_then_subagent")
    conv, child = scenario.conversation_id, scenario.child_id
    projector, _ = project(parse(scenario).entries + parse_child(scenario).entries)
    by_record = projector.usage_by_record()
    assert by_record[child].messages == 4
    assert by_record[child].usage.input_tokens == 6000 + 6100 + 6300 + 6400
    assert by_record[child].usage.output_tokens == 40 + 12 + 20 + 3
    parent_prompts = [
        e.usage.input_tokens for e in parse(scenario).entries if isinstance(e, TokenUsage)
    ]
    assert by_record[MAIN_RECORD].usage.input_tokens == sum(parent_prompts)
    turns = projector.usage_by_turn()
    assert (
        sum(t.usage.input_tokens for t in turns.values())
        == by_record[MAIN_RECORD].usage.input_tokens
    )
    assert all(child not in name for name in turns)
    assert conv in next(iter(turns))


def test_child_wakes_the_parent_through_a_notice_that_opens_a_turn() -> None:
    scenario = load("cancel_then_subagent")
    conv, child = scenario.conversation_id, scenario.child_id
    entries = parse(scenario).entries
    notice = only([e for e in entries if isinstance(e, Notice)])
    assert notice.key == f"agy:{conv}:7" and notice.text == "CHILD_OK"
    assert notice.cause is HarnessCause.BACKGROUND_TASK
    _, projection = project(entries + parse_child(scenario).entries)
    woken = projection.turns_observed[-1]
    assert woken.origin is TurnOrigin.HARNESS and woken.cause is HarnessCause.BACKGROUND_TASK
    reply = [d for d in drafts_of(projection, DraftKind.AGENT_CHAT) if d.order.seq == 8]
    assert only(reply).turn == woken.turn and child in only(reply).payload
    child_notices = [
        d for d in projection.drafts if d.record == child and d.kind is DraftKind.SYSTEM_MARKER
    ]
    assert child_notices == []


# meiva:note
# CONSTRUCTED rows (s7/s8).
def test_parallel_calls_pair_with_their_outputs_by_offset() -> None:
    scenario = load("timer_and_compaction")
    conv = scenario.conversation_id
    entries = parse(scenario).entries
    calls = [e for e in entries if isinstance(e, ToolCall)]
    assert [(c.call_id, c.name) for c in calls] == [
        (f"agy:{conv}:1:0", "schedule"),
        (f"agy:{conv}:1:1", "run_command"),
    ]
    results = [e for e in entries if isinstance(e, ToolResult)]
    assert [(r.call_id, r.status) for r in results] == [
        (f"agy:{conv}:1:0", ToolStatus.BACKGROUND),
        (f"agy:{conv}:1:1", ToolStatus.OK),
    ]


def test_timer_wake_up_opens_a_turn_of_its_own() -> None:
    entries = parse(load("timer_and_compaction")).entries
    notice = only([e for e in entries if isinstance(e, Notice)])
    assert notice.cause is HarnessCause.SCHEDULED_WAKE
    assert notice.text == "Reply with the single word TIMER_FIRED."
    _, projection = project(entries)
    timer_turn = next(
        o for o in projection.turns_observed if o.cause is HarnessCause.SCHEDULED_WAKE
    )
    assert timer_turn.origin is TurnOrigin.HARNESS
    reply = next(
        d for d in drafts_of(projection, DraftKind.AGENT_CHAT) if d.payload == "TIMER_FIRED"
    )
    assert reply.turn == timer_turn.turn


# meiva:note
# CONSTRUCTED rows (s8); the checkpoint text is a truncated trajectory summary.
def test_checkpoint_is_a_compaction_entry_and_the_restart_notice_is_skipped() -> None:
    scenario = load("timer_and_compaction")
    conv = scenario.conversation_id
    entries = parse(scenario).entries
    assert f"agy:{conv}:8" not in keys_of(entries)
    compaction = only([e for e in entries if isinstance(e, Compaction)])
    assert compaction.key == f"agy:{conv}:9" and compaction.summary
    humans = [e for e in entries if isinstance(e, HumanInput)]
    assert humans[-1].text == "/compact"


def test_compaction_shows_a_marker_and_its_spend_belongs_to_the_compact_turn() -> None:
    entries = parse(load("timer_and_compaction")).entries
    projector, projection = project(entries, sent("c", "w-c", "/compact"))
    marker = [d for d in drafts_of(projection, DraftKind.SYSTEM_MARKER) if d.order.seq == 9]
    assert only(marker).payload == "status:Context compacted"
    compact_turn = next(d.turn for d in projection.deliveries if d.input_key == "c")
    spent = projector.usage_by_turn()[compact_turn or ""]
    assert (spent.usage.input_tokens, spent.usage.output_tokens) == (12775, 3196)


def test_summed_row_usage_equals_what_the_acp_prompt_returned() -> None:
    for name in ("basic_turn", "thinking_turn", "image_input"):
        scenario = load(name)
        acp = scenario.source["acp_usage"]
        assert isinstance(acp, dict)
        projector, _ = project(parse(scenario).entries)
        tallies = list(projector.usage_by_turn().values())
        assert len(tallies) == 1 and tallies[0].reliable
        assert tallies[0].usage.input_tokens == acp["inputTokens"], name
        assert tallies[0].usage.output_tokens == acp["outputTokens"], name
    scenario = load("basic_turn")
    usage = [e.usage for e in parse(scenario).entries if isinstance(e, TokenUsage)]
    assert [(u.input_tokens, u.output_tokens) for u in usage] == [(11111, 49), (11253, 17)]


def test_cached_tokens_inside_the_prompt_are_not_added_and_beside_it_are() -> None:
    def usage_of(prompt: int, cached: int) -> TokenUsage:
        row = {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-12T08:56:39Z",
            "content": "x",
            "input_tokens": prompt,
            "cache_read_tokens": cached,
            "output_tokens": 2,
        }
        entries = parse_rows([row], conversation_id="c").entries
        return only([e for e in entries if isinstance(e, TokenUsage)])

    inside = usage_of(1000, 400).usage
    assert (inside.input_tokens, inside.cache_read_input_tokens) == (1000, 400)
    beside = usage_of(100, 400).usage
    assert (beside.input_tokens, beside.cache_read_input_tokens) == (500, 400)


def test_image_input_reaches_the_timeline_through_the_sent_part() -> None:
    scenario = load("image_input")
    human = only([e for e in parse(scenario).entries if isinstance(e, HumanInput)])
    assert human.text == "What single colour is this image? One word."
    message = sent("img-in", "w-img", human.text, images=("img://red",))
    _, projection = project(parse(scenario).entries, message)
    shown = only(drafts_of(projection, DraftKind.INBOUND_CHAT))
    assert shown.images == ("img://red",) and shown.part_ref == "img-in-part"
    assert only(list(projection.deliveries)).basis is MatchBasis.TEXT
    answer = only(drafts_of(projection, DraftKind.AGENT_CHAT))
    assert answer.payload == "Red"


def test_unknown_row_type_is_skipped_and_reported_once() -> None:
    rows = [
        {
            "step_index": 0,
            "type": "ERROR_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:00Z",
            "content": "boom",
        },
        {
            "step_index": 1,
            "type": "ERROR_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:01Z",
            "content": "boom",
        },
        {
            "step_index": 2,
            "type": "EPHEMERAL_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:02Z",
        },
    ]
    entries = parse_rows(rows, conversation_id="c").entries
    assert [(e.anomaly, e.detail) for e in entries if isinstance(e, Anomaly)] == [
        (AnomalyKind.UNKNOWN_TYPE, "ERROR_MESSAGE"),
        (AnomalyKind.UNKNOWN_TYPE, "ERROR_MESSAGE"),
        (AnomalyKind.UNKNOWN_TYPE, "EPHEMERAL_MESSAGE"),
    ]
    _, projection = project(entries)
    assert [(r.anomaly, r.detail) for r in projection.anomalies] == [
        (AnomalyKind.UNKNOWN_TYPE, "ERROR_MESSAGE"),
        (AnomalyKind.UNKNOWN_TYPE, "EPHEMERAL_MESSAGE"),
    ]
    assert projection.drafts == ()


def test_known_type_missing_a_required_field_is_reported_not_raised() -> None:
    stamp = "2026-11-12T08:00:00Z"
    rows: list[object] = [
        {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "created_at": stamp},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": stamp,
            "tool_calls": [{"args": {}}],
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "status": "DONE",
            "created_at": stamp,
            "content": "orphan output",
        },
        {"step_index": 3, "type": "CHECKPOINT", "status": "DONE", "created_at": stamp},
        {
            "step_index": 4,
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": stamp,
            "content": "[Notice] something new",
        },
        {
            "step_index": 5,
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "yesterday",
            "content": "x",
        },
        {"type": "USER_INPUT", "status": "DONE", "created_at": stamp, "content": "x"},
        "not a row",
    ]
    result = parse_rows(rows, conversation_id="c")
    found = [(e.anomaly, e.detail) for e in result.entries if isinstance(e, Anomaly)]
    assert found == [
        (AnomalyKind.MISSING_FIELD, "USER_INPUT.content"),
        (AnomalyKind.MISSING_FIELD, "PLANNER_RESPONSE.tool_calls[0].name"),
        (AnomalyKind.MISSING_FIELD, "GENERIC.tool_call"),
        (AnomalyKind.MISSING_FIELD, "CHECKPOINT.content"),
        (AnomalyKind.UNKNOWN_TYPE, "system_message:[Notice]"),
        (AnomalyKind.MISSING_FIELD, "created_at"),
        (AnomalyKind.MISSING_FIELD, "step_index"),
        (AnomalyKind.MISSING_FIELD, "row"),
    ]
    assert len(result.entries) == len(rows)
    assert len(set(keys_of(result.entries))) == len(rows)
    assert project(result.entries)[1].drafts == ()


def test_a_row_status_the_parser_does_not_know_is_reported() -> None:
    stamp = "2026-11-12T08:00:00Z"
    planner: dict[str, object] = {
        "step_index": 0,
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": stamp,
        "tool_calls": [{"name": "run_command", "args": {}}],
    }
    weird = {
        "step_index": 1,
        "type": "GENERIC",
        "status": "WEIRD",
        "created_at": stamp,
        "content": "x",
    }
    entries = parse_rows([planner, weird], conversation_id="c").entries
    assert [(e.anomaly, e.detail) for e in entries if isinstance(e, Anomaly)] == [
        (AnomalyKind.UNKNOWN_TYPE, "status:WEIRD")
    ]
    failed = {**weird, "status": "ERROR"}
    entries = parse_rows([planner, failed], conversation_id="c").entries
    assert only([e for e in entries if isinstance(e, ToolResult)]).status is ToolStatus.ERROR


def test_order_is_step_and_sub_and_created_at_is_only_displayed() -> None:
    rows = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:05Z",
            "content": "<USER_REQUEST>a</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:04Z",
            "content": "b",
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-12T08:00:09Z",
            "content": "c",
        },
    ]
    entries = parse_rows(rows, conversation_id="c").entries
    assert [(e.order.seq, e.order.sub) for e in entries] == [(0, 0), (1, 1), (3, 1)]
    assert entries[0].at == "2026-11-12T08:00:05Z" and entries[1].at == "2026-11-12T08:00:04Z"


def test_feeding_the_same_entries_twice_projects_nothing_the_second_time() -> None:
    for name in ("steer_background", "cancel_then_subagent", "timer_and_compaction"):
        scenario = load(name)
        entries = (
            parse(scenario).entries + parse_child(scenario).entries
            if scenario.child_rows
            else parse(scenario).entries
        )
        projector, first = project(entries)
        assert first.drafts
        assert projector.feed(entries) == Projection()
        assert projector.feed(parse(scenario).entries) == Projection()


def test_resuming_from_a_saved_state_gives_the_same_entries_as_one_pass() -> None:
    scenario = load("steer_background")
    conv = scenario.conversation_id
    whole = parse(scenario)
    for cut in range(len(scenario.rows) + 1):
        head = parse_rows(scenario.rows[:cut], conversation_id=conv, side=scenario.side)
        tail = parse_rows(
            scenario.rows[cut:], conversation_id=conv, state=head.state, side=scenario.side
        )
        assert head.entries + tail.entries == whole.entries
        assert tail.state.last_step == whole.state.last_step
