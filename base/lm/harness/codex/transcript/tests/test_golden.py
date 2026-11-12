from __future__ import annotations

import copy

from base.lm.harness.codex.transcript.parse import (
    parse_items_page,
    parse_rollout,
    parse_rollout_line,
    parse_thread_read,
)
from base.lm.harness.codex.transcript.tests.support import (
    CHILD_THREAD,
    LATE_COMMAND,
    SPAWN_CALL,
    as_obj,
    in_order,
    kinds,
    load_json,
    load_lines,
    page_rows,
    project,
    row_item,
    sent,
    thread_turns,
    turn_items,
)
from base.lm.harness.events import HarnessCause, TurnOrigin, Usage
from base.lm.harness.session.transcript.drafts import (
    AnomalyKind,
    InboundPart,
    MatchBasis,
    SentInput,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
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
    TurnEnd,
)

TURN_S1_A = "01a1cef8-fcef-7122-823f-3ed2bdf1ba67"
TURN_S1_B = "01a1cef9-2112-77a0-9106-2044cce3c68a"
TURN_S2 = "01a1cef9-b24f-7d73-961b-a77a83fbadc5"
TURN_S3_A = "01a1cefa-641e-7311-b99e-8c19287d1fa1"
TURN_S3_B = "01a1cefa-87b2-7b52-91ed-5653b7e03aae"
TURN_COMPACT = "01a1cf00-2fe3-7963-85ea-d3a532d06351"
TURN_RESUMED = "01a1cf00-1ed9-79c0-9063-f8f7630a481c"
PARENT_TURN = "01a1cefe-1b10-7702-84c6-59faba75cdf8"
CHILD_TURN = "01a1cefe-5764-7421-8a79-30cff8efdc74"
SLEPT = "exec-aeab7c02-5980-467b-af17-d3f9424428a4"


def _of[T](entries: list[AnyEntry], kind: type[T]) -> list[T]:
    return [entry for entry in entries if isinstance(entry, kind)]


def _sorted_by_key(drafts: list[TimelineDraft]) -> list[TimelineDraft]:
    return sorted(drafts, key=lambda draft: draft.key)


def test_a_plain_turn_reads_reasoning_a_command_and_the_answer() -> None:
    entries = parse_items_page(load_json("basic/items.json"))
    _, projection = project(entries, sent("cid-s1-a", "first"), sent("cid-s1-b", "second"))
    first = [draft for draft in in_order(projection) if draft.turn == TURN_S1_A]
    assert [draft.kind.value for draft in first] == [
        "inbound_chat",
        "agent_chat",
        "agent_code",
        "code_output",
        "agent_chat",
    ]
    call, output = first[2], first[3]
    assert call.tool_name == "command"
    assert call.tool_input == {
        "command": "/bin/zsh -lc 'echo hello-cx'",
        "cwd": "/work",
    }
    assert call.tool_use_id == output.tool_use_id == "exec-410fef3c-c1cd-4fb8-b7a0-d3611d48fe2d"
    assert output.payload == "hello-cx\n"
    assert output.tool_status is ToolStatus.OK
    assert output.exec_ms == 0
    assert first[1].payload.startswith("\u6211\u5148\u8ba1\u7b97")
    # meiva:note
    # Reasoning is encrypted: the entry says it happened, the timeline shows nothing for it.
    assert [entry.text for entry in _of(entries, Reasoning)] == ["", "", ""]
    assert not any(draft.kind.value == "agent_reasoning" for draft in projection.drafts)


def test_the_history_and_the_rollout_of_one_thread_give_the_same_drafts() -> None:
    inputs = (sent("cid-s1-a", "first"), sent("cid-s1-b", "second"))
    _, from_history = project(parse_items_page(load_json("basic/items.json")), *inputs)
    rollout = parse_rollout(load_lines("basic/rollout.jsonl"), record=MAIN_RECORD)
    _, from_rollout = project(rollout, *inputs)
    assert [draft.key for draft in in_order(from_history)] == [
        draft.key for draft in in_order(from_rollout)
    ]
    assert [draft.payload for draft in in_order(from_history)] == [
        draft.payload for draft in in_order(from_rollout)
    ]
    assert [closed.turn for closed in from_rollout.turns_closed] == [TURN_S1_A, TURN_S1_B]


def test_the_rollout_supplies_usage_per_sample_and_per_turn() -> None:
    rollout = parse_rollout(load_lines("basic/rollout.jsonl"), record=MAIN_RECORD)
    projector, _ = project(rollout)
    samples = _of(rollout, TokenUsage)
    assert len(samples) == 3
    assert all(sample.message_ref.startswith("resp_") for sample in samples)
    by_turn = projector.usage_by_turn()
    assert by_turn[TURN_S1_A].usage == Usage(
        input_tokens=30284, output_tokens=207, cache_read_input_tokens=26368
    )
    assert by_turn[TURN_S1_A].messages == 2
    assert by_turn[TURN_S1_B].usage == Usage(
        input_tokens=20461, output_tokens=28, cache_read_input_tokens=11520
    )
    assert by_turn[TURN_S1_B].reliable


def test_b_a_text_image_text_input_expands_into_the_messages_it_carried() -> None:
    entries = parse_items_page(load_json("basic/items.json"))
    (input_b,) = [entry for entry in _of(entries, HumanInput) if entry.wire_id == "cid-s1-b"]
    assert (
        input_b.text
        == "Part one: the picture below is a solid color.\nPart two: name that color in one word."
    )
    combined = SentInput(
        key="input-b",
        wire_id="cid-s1-b",
        text=input_b.text,
        parts=(
            InboundPart("msg-1", "Part one: the picture below is a solid color.", ("img-1",)),
            InboundPart("msg-2", "Part two: name that color in one word."),
        ),
    )
    _, projection = project(entries, sent("cid-s1-a", "first"), combined)
    shown = [
        draft
        for draft in in_order(projection)
        if draft.kind.value == "inbound_chat" and draft.turn == TURN_S1_B
    ]
    assert [(draft.part_ref, draft.slot, draft.images) for draft in shown] == [
        ("msg-1", 0, ("img-1",)),
        ("msg-2", 1, ()),
    ]
    assert shown[0].key == f"{input_b.key}#msg-1"
    delivery = projection.deliveries[-1]
    assert (delivery.input_key, delivery.basis, delivery.turn) == (
        "input-b",
        MatchBasis.WIRE_ID,
        TURN_S1_B,
    )
    assert not delivery.joined_running_turn


def test_c_a_steer_joins_the_turn_it_was_sent_into() -> None:
    inputs = (sent("cid-s2-a", "run"), sent("cid-s2-steer", "also"))
    for entries in (
        parse_thread_read(load_json("steer/thread.json")),
        parse_rollout(load_lines("steer/rollout.jsonl"), record=MAIN_RECORD),
    ):
        _, projection = project(entries, *inputs)
        first, steer = projection.deliveries
        assert (first.input_key, first.joined_running_turn) == ("input-cid-s2-a", False)
        assert (steer.input_key, steer.joined_running_turn) == ("input-cid-s2-steer", True)
        assert first.turn == steer.turn == TURN_S2
        assert projection.unmatched_inputs == ()
        assert [(o.turn, o.origin) for o in projection.turns_observed] == [
            (TURN_S2, TurnOrigin.INPUT)
        ]
        assert kinds(projection) == [
            "inbound_chat",
            "agent_code",
            "code_output",
            "inbound_chat",
            "agent_chat",
        ]


def test_d_an_interrupt_closes_its_turn_and_the_lost_steer_never_appears() -> None:
    entries = parse_thread_read(load_json("interrupt/thread.json"))
    assert [entry.turn for entry in _of(entries, Interrupt)] == [TURN_S3_A, TURN_S3_B]
    lost_steer = sent("cid-s3-steer", "also say STEERED")
    _, projection = project(entries, sent("cid-s3-a", "run"), sent("cid-s3-b", "write"), lost_steer)
    assert [(closed.turn, closed.reason) for closed in projection.turns_closed] == [
        (TURN_S3_A, "interrupted"),
        (TURN_S3_B, "interrupted"),
    ]
    markers = [draft for draft in projection.drafts if draft.kind.value == "system_marker"]
    assert [(draft.payload, draft.turn) for draft in markers] == [
        ("status:Turn interrupted", TURN_S3_A),
        ("status:Turn interrupted", TURN_S3_B),
    ]
    assert {delivery.input_key for delivery in projection.deliveries} == {
        "input-cid-s3-a",
        "input-cid-s3-b",
    }
    assert not any("STEERED" in draft.payload for draft in projection.drafts)
    # meiva:note
    # The streamed half answer of the second turn was never recorded, so that turn shows no agent text.
    assert [draft.kind.value for draft in in_order(projection) if draft.turn == TURN_S3_B] == [
        "inbound_chat",
        "system_marker",
    ]


def _late_command_arrivals() -> tuple[list[AnyEntry], list[AnyEntry], list[AnyEntry]]:
    entries = parse_thread_read(load_json("interrupt/thread.json"))
    late = [
        entry
        for entry in entries
        if isinstance(entry, ToolCall | ToolResult) and entry.call_id == LATE_COMMAND
    ]
    others = [entry for entry in entries if entry not in late]
    next_turn = [entry for entry in others if entry.turn == TURN_S3_B]
    first_turn = [entry for entry in others if entry.turn == TURN_S3_A]
    after_next_turn = first_turn + next_turn + late
    result_before_call = first_turn + next_turn + late[::-1]
    return entries, after_next_turn, result_before_call


def test_e_a_command_that_finished_late_sorts_back_into_its_own_turn() -> None:
    entries, after_next_turn, result_before_call = _late_command_arrivals()
    inputs = (sent("cid-s3-a", "run"), sent("cid-s3-b", "write"))
    _, natural = project(entries, *inputs)
    _, late = project(after_next_turn, *inputs)
    _, reversed_result = project(result_before_call, *inputs)
    ordered = in_order(late)
    position = {draft.key: index for index, draft in enumerate(ordered)}
    next_user = next(draft for draft in ordered if draft.turn == TURN_S3_B)
    call = ordered[position[f"call:{LATE_COMMAND}"]]
    output = ordered[position[f"result:{LATE_COMMAND}"]]
    assert call.turn == output.turn == TURN_S3_A
    assert position[call.key] < position[output.key] < position[next_user.key]
    assert output.exec_ms == 29889
    assert _sorted_by_key(list(natural.drafts)) == _sorted_by_key(list(late.drafts))
    assert _sorted_by_key(list(natural.drafts)) == _sorted_by_key(list(reversed_result.drafts))
    assert [draft.key for draft in in_order(natural)] == [draft.key for draft in ordered]


def test_e_a_background_command_that_outlived_its_turn_reads_before_the_final_answer() -> None:
    entries = parse_items_page(load_json("late_command/items.json"))
    # meiva:note
    # The page lists the command last, because it finished last; its start puts it before the reply.
    assert [type(entry).__name__ for entry in entries if not isinstance(entry, Reasoning)][-2:] == [
        "ToolCall",
        "ToolResult",
    ]
    _, projection = project(entries, sent("cid-s4-a", "start"))
    assert kinds(projection) == ["inbound_chat", "agent_code", "code_output", "agent_chat"]
    rollout = parse_rollout(load_lines("late_command/rollout.jsonl"), record=MAIN_RECORD)
    ends = [index for index, entry in enumerate(rollout) if isinstance(entry, TurnEnd)]
    calls = [index for index, entry in enumerate(rollout) if isinstance(entry, ToolCall)]
    assert ends and calls and calls[0] > ends[0]
    _, from_rollout = project(rollout, sent("cid-s4-a", "start"))
    assert kinds(from_rollout) == kinds(projection)
    assert {draft.turn for draft in from_rollout.drafts} == {"01a1cefd-5933-7f40-8255-8ba5b4a29838"}


def test_f_the_two_writes_of_one_message_make_one_draft() -> None:
    rollout = parse_rollout(load_lines("steer/rollout.jsonl"), record=MAIN_RECORD)
    answer = "agent:msg_0d6e9366dadb000c016af57f83c51487d0a488a8e85a80612d"
    assert [entry.key for entry in _of(rollout, AgentText)].count(answer) == 2
    reasoning_keys = [entry.key for entry in _of(rollout, Reasoning)]
    assert len(reasoning_keys) == 4 and len(set(reasoning_keys)) == 2
    _, projection = project(rollout, sent("cid-s2-a", "run"), sent("cid-s2-steer", "also"))
    assert [draft.key for draft in projection.drafts].count(answer) == 1
    assert len([draft for draft in projection.drafts if draft.kind.value == "agent_chat"]) == 1


def test_g_a_sub_agent_nests_under_the_call_that_spawned_it() -> None:
    parent = parse_thread_read(load_json("subagent/parent.json"))
    child = parse_thread_read(load_json("subagent/child.json"), record=CHILD_THREAD)
    (link,) = _of(parent, SubagentLink)
    assert (link.call_id, link.child_record) == (SPAWN_CALL, CHILD_THREAD)
    spawn = next(entry for entry in _of(parent, ToolCall) if entry.call_id == SPAWN_CALL)
    assert spawn.name == "spawnAgent"
    parent_first = project(parent + child, sent("cid-s5-a", "spawn"))
    child_first = project(child + parent, sent("cid-s5-a", "spawn"))
    for projector, projection in (parent_first, child_first):
        nested = [draft for draft in in_order(projection) if draft.record == CHILD_THREAD]
        assert {draft.parent_tool_use_id for draft in nested} == {SPAWN_CALL}
        assert [draft.kind.value for draft in nested] == [
            "subagent_text",
            "agent_code",
            "code_output",
            "subagent_text",
        ]
        assert {draft.turn for draft in nested} == {PARENT_TURN}
        assert not any(
            draft.record == MAIN_RECORD and draft.parent_tool_use_id for draft in projection.drafts
        )
        assert projector.unlinked_records() == ()
    assert _sorted_by_key(list(parent_first[1].drafts)) == _sorted_by_key(
        list(child_first[1].drafts)
    )


def test_g_a_child_that_arrives_before_its_link_is_held_then_nested() -> None:
    parent = parse_thread_read(load_json("subagent/parent.json"))
    child = parse_thread_read(load_json("subagent/child.json"), record=CHILD_THREAD)
    projector, first = project(child)
    assert first.drafts == ()
    assert projector.unlinked_records() == (CHILD_THREAD,)
    second = projector.feed(parent)
    assert projector.unlinked_records() == ()
    assert {
        draft.parent_tool_use_id for draft in second.drafts if draft.record == CHILD_THREAD
    } == {SPAWN_CALL}


def test_g_the_sub_agent_report_is_a_notice_and_its_spend_stays_apart() -> None:
    rollout = parse_rollout(load_lines("subagent/rollout.jsonl"), record=MAIN_RECORD)
    (notice,) = _of(rollout, Notice)
    assert notice.cause is HarnessCause.BACKGROUND_TASK
    assert notice.text == (
        '{"agent_path":"01a1cefe-574e-79f2-a985-5e66bedfa3a6","status":{"completed":"BETA"}}'
    )
    # meiva:note
    # The child's own rollout was not recorded. A sample of the parent's file read as the child's
    # (with its own response id) stands in for it, to show the record keeps the two tallies apart.
    sample = next(
        line
        for line in load_lines("subagent/rollout.jsonl")
        if line["type"] == "token_usage_record"
    )
    child_sample = copy.deepcopy(sample)
    as_obj(child_sample["payload"])["response_id"] = "resp_child_1"
    child_usage = parse_rollout_line(child_sample, record=CHILD_THREAD)
    projector, projection = project(rollout + child_usage)
    assert projector.usage_by_record()[MAIN_RECORD].messages == 5
    assert projector.usage_by_record()[CHILD_THREAD].messages == 1
    assert projector.usage_by_turn()[PARENT_TURN].messages == 5
    notices = [draft for draft in projection.drafts if draft.kind.value == "system_marker"]
    assert [draft.source for draft in notices] == ["background_task"]
    assert [
        observed.cause
        for observed in projection.turns_observed
        if observed.origin is TurnOrigin.HARNESS
    ] == [HarnessCause.BACKGROUND_TASK]


def test_h_a_compaction_is_a_marker_in_its_own_turn_with_its_own_spend() -> None:
    entries = parse_thread_read(load_json("fork/fork.json"))
    (compaction,) = _of(entries, Compaction)
    assert compaction.turn == TURN_COMPACT
    rollout = parse_rollout(load_lines("fork/rollout.jsonl"), record=MAIN_RECORD)
    projector, projection = project(entries + rollout, sent("cid-s7-a", "resume"))
    markers = [draft for draft in projection.drafts if draft.kind.value == "system_marker"]
    assert [(draft.payload, draft.turn) for draft in markers] == [
        ("status:Context compacted", TURN_COMPACT)
    ]
    assert projector.usage_by_turn()[TURN_COMPACT].usage.output_tokens == 308
    assert [closed.reason for closed in projection.turns_closed if closed.turn == TURN_COMPACT] == [
        "completed"
    ]


def test_i_an_unknown_item_type_is_skipped_and_reported_once_per_type() -> None:
    page = copy.deepcopy(load_json("basic/items.json"))
    rows = page_rows(page)
    row_item(rows[1])["type"] = "holographicNote"
    row_item(rows[4])["type"] = "holographicNote"
    row_item(rows[5])["type"] = "webSearch"
    entries = parse_items_page(page)
    assert {entry.detail for entry in _of(entries, Anomaly)} == {"holographicNote", "webSearch"}
    projector, projection = project(entries, sent("cid-s1-a", "first"), sent("cid-s1-b", "second"))
    assert sorted((a.anomaly, a.detail) for a in projection.anomalies) == [
        (AnomalyKind.UNKNOWN_TYPE, "holographicNote"),
        (AnomalyKind.UNKNOWN_TYPE, "webSearch"),
    ]
    assert not any("holographic" in draft.payload for draft in projection.drafts)
    again = projector.feed(entries)
    assert again.anomalies == () and again.drafts == ()
    _, fresh = project(parse_items_page(page))
    assert len(fresh.anomalies) == 2


def test_i_a_known_type_without_a_field_it_needs_is_a_missing_field() -> None:
    page = copy.deepcopy(load_json("basic/items.json"))
    rows = page_rows(page)
    del row_item(rows[2])["text"]
    row_item(rows[3])["status"] = "teleported"
    del rows[6]["turnId"]
    entries = parse_items_page(page)
    found = {(entry.anomaly, entry.detail) for entry in _of(entries, Anomaly)}
    assert found == {
        (AnomalyKind.MISSING_FIELD, "text"),
        (AnomalyKind.UNKNOWN_TYPE, "commandExecution.status=teleported"),
        (AnomalyKind.MISSING_FIELD, "turnId"),
    }
    _, projection = project(entries, sent("cid-s1-a", "first"), sent("cid-s1-b", "second"))
    assert len(projection.anomalies) == 3
    assert [draft.kind.value for draft in in_order(projection)] == [
        "inbound_chat",
        "agent_chat",
        "agent_chat",
    ]


def test_a_running_command_shows_its_call_and_gains_its_result_when_it_finishes() -> None:
    page = copy.deepcopy(load_json("late_command/items.json"))
    command = next(row for row in page_rows(page) if row_item(row)["type"] == "commandExecution")
    finished = copy.deepcopy(command)
    row_item(command).update(
        status="inProgress",
        aggregatedOutput=None,
        exitCode=None,
        durationMs=None,
        completedAtMs=None,
    )
    command["completedAtMs"] = None
    running = parse_items_page(page)
    assert [
        type(entry).__name__ for entry in running if isinstance(entry, ToolCall | ToolResult)
    ] == ["ToolCall"]
    projector, first = project(running, sent("cid-s4-a", "start"))
    assert "code_output" not in [draft.kind.value for draft in first.drafts]
    second = projector.feed(parse_items_page({"data": [finished]}))
    assert [(draft.kind.value, draft.payload) for draft in second.drafts] == [
        ("code_output", "BGLATE\n")
    ]
    assert second.drafts[0].exec_ms == 13890


def test_j_feeding_the_same_entries_twice_projects_nothing_the_second_time() -> None:
    entries = parse_thread_read(load_json("steer/thread.json"))
    entries += parse_rollout(load_lines("steer/rollout.jsonl"), record=MAIN_RECORD)
    projector, first = project(entries, sent("cid-s2-a", "run"), sent("cid-s2-steer", "also"))
    assert first.drafts and len(first.deliveries) == 2
    second = projector.feed(entries)
    assert second.drafts == () and second.deliveries == ()
    assert second.turns_observed == () and second.turns_closed == ()
    assert parse_thread_read(load_json("steer/thread.json")) == parse_thread_read(
        load_json("steer/thread.json")
    )


def test_k_a_fork_projects_only_what_the_parent_did_not_already_show() -> None:
    parent = parse_thread_read(load_json("fork/parent.json"))
    forked = parse_thread_read(load_json("fork/fork.json"))
    assert {entry.key for entry in parent} < {entry.key for entry in forked}
    _, projection = project(
        forked, sent("cid-s7-a", "resume"), known_keys=[entry.key for entry in parent]
    )
    assert kinds(projection) == ["inbound_chat", "agent_chat", "system_marker"]
    assert {draft.turn for draft in projection.drafts} == {TURN_RESUMED, TURN_COMPACT}
    assert [closed.turn for closed in projection.turns_closed] == [TURN_RESUMED, TURN_COMPACT]
    (delivery,) = projection.deliveries
    assert delivery.input_key == "input-cid-s7-a"


def test_a_page_that_starts_mid_turn_reads_a_steer_with_the_turns_already_seen() -> None:
    turn = thread_turns(load_json("steer/thread.json"))[0]
    items = turn_items(turn)
    rows = [{"turnId": turn["id"], "item": item} for item in items]
    tail = rows[1:]
    cold = _of(parse_items_page({"data": tail}), HumanInput)
    warm = _of(parse_items_page({"data": tail}, input_turns={str(turn["id"])}), HumanInput)
    assert [entry.joined_running_turn for entry in cold] == [False]
    assert [entry.joined_running_turn for entry in warm] == [True]
    whole = _of(parse_items_page({"data": rows}), HumanInput)
    assert [entry.joined_running_turn for entry in whole] == [False, True]
