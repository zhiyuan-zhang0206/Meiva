from __future__ import annotations

from base.lm.harness.claude.transcript.parse import ParseState, parse_row, row_version
from base.lm.harness.claude.transcript.tests.support import (
    entries_of,
    in_order,
    kinds,
    parse_all,
    project,
    result_usage,
    rows,
    sent,
    source,
    total,
)
from base.lm.harness.events import HarnessCause, TurnOrigin
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    InboundPart,
    MatchBasis,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import (
    AnomalyKind,
    AnyEntry,
    HumanInput,
    Interrupt,
    Order,
    SubagentLink,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolStatus,
)

BASIC = "3c3d2de5-c479-52b7-91a6-fce4e4d26db9"
MULTI = "b54d7410-9b85-5483-84e1-27e497a0447f"
STEER_FIRST = "b4b83572-ea45-5c6c-a2ae-602baabae023"
STEER_LATE = "632a7901-371a-5d8b-81ee-b2dd27082c60"
INT_ONE = "48416559-6f06-590a-94ed-0417ad07eb53"
INT_TWO = "a2e1e591-fcd0-5b0d-b3de-170e26f24276"
INT_THREE = "e6fa562d-46b8-5c1e-8725-1d4ed2623571"
KILL_ONE = "f245410f-5fd9-5831-b8bf-24304543c496"
KILL_TWO = "bc9c6287-ef98-5d8a-b454-73fe32801d6a"
RESUMED = "c9638f3a-6d41-57db-bbaa-8beaa07ccee5"
DENIED = "1501af5d-4a7a-56ea-9947-568e4ff6c11e"
SUBS = "611dfcac-5809-5de8-b8c6-2e185bf7d5be"
COMPACT = "e2743bac-c2d9-5409-9758-cf75220290d6"
FORKED = "5b9f1a0e-10ce-5ce8-961b-7cc3c470dbab"
SYNC_AGENT, ASYNC_AGENT = "af6798fb938fb227f", "a0044dfe6d0ac4533"
SYNC_CALL, ASYNC_CALL = "toolu_01Uox3S5dZb2kdxdKYQ4G9T8", "toolu_01WduaMfttYL3vz9k74joMjx"


def test_basic_turn_projects_in_transcript_order_with_stable_keys() -> None:
    entries = entries_of("basic")
    projector, projection = project(entries, [sent(BASIC, "k1", "Think briefly")])
    drafts = in_order(projection.drafts)
    assert kinds(drafts) == [
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert [(d.key) for d in drafts[:1]] == [f"claude:{BASIC}#k1-row"]
    assert len({d.key for d in drafts}) == len(drafts)
    call, output = drafts[2], drafts[3]
    assert call.tool_use_id == output.tool_use_id == "toolu_019RvAbbVJvfTqoKBx7uPcf5"
    assert call.tool_name == "Bash"
    assert call.tool_input == {
        "command": "echo hello-transcript && date +%s",
        "description": call.tool_input["description"] if call.tool_input else "",
    }
    assert output.payload == "hello-transcript\n1794473821"
    assert output.exec_ms is not None and 0 < output.exec_ms < 1000
    assert drafts[-1].payload.startswith("\u547d\u4ee4\u8f93\u51fa\u4e3a")
    assert projector.unanswered_calls() == ()


def test_basic_turn_delivers_its_input_and_opens_and_closes_one_turn() -> None:
    projector, projection = project(entries_of("basic"), [sent(BASIC, "k1", "Think briefly")])
    (delivery,) = projection.deliveries
    assert (delivery.input_key, delivery.basis, delivery.joined_running_turn) == (
        "k1",
        MatchBasis.WIRE_ID,
        False,
    )
    assert [(t.origin, t.cause) for t in projection.turns_observed] == [(TurnOrigin.INPUT, None)]
    assert [t.reason for t in projection.turns_closed] == ["completed"]
    assert projection.anomalies == () and projection.unmatched_inputs == ()
    assert projector.undelivered() == ()


def test_usage_sums_once_per_message_and_equals_the_result_message() -> None:
    entries = entries_of("basic")
    rows_of_usage = [e for e in entries if isinstance(e, TokenUsage)]
    assert len(rows_of_usage) == 4
    assert len({e.message_ref for e in rows_of_usage}) == 2
    projector, _ = project(entries, [sent(BASIC, "k1", "x")])
    (tally,) = projector.usage_by_turn().values()
    assert tally.messages == 2 and tally.reliable
    assert tally.usage == result_usage("basic", 0)


def test_multiblock_input_expands_to_its_original_messages_with_images_on_the_right_one() -> None:
    parts = [
        InboundPart(ref="row-1", text="First block: look at the next image.", images=("img-1",)),
        InboundPart(ref="row-2", text="Last block: name the dominant color in one word."),
    ]
    _, projection = project(entries_of("multiblock"), [sent(MULTI, "k", "ignored", parts)])
    inbound = [d for d in in_order(projection.drafts) if d.kind.value == "inbound_chat"]
    assert [(d.part_ref, d.images) for d in inbound] == [("row-1", ("img-1",)), ("row-2", ())]
    assert [d.key for d in inbound] == [f"claude:{MULTI}#row-1", f"claude:{MULTI}#row-2"]
    assert projection.anomalies == ()
    assert kinds(in_order(projection.drafts))[-2:] == ["agent_reasoning", "agent_chat"]


def test_a_steer_absorbed_mid_turn_is_delivered_by_its_queued_command_row() -> None:
    entries = entries_of("steer")
    steer_entry = next(e for e in entries if isinstance(e, HumanInput) and e.wire_id == STEER_LATE)
    assert steer_entry.joined_running_turn and steer_entry.key.startswith("claude:cda538d9")
    inputs = [sent(STEER_FIRST, "first", "run"), sent(STEER_LATE, "steer", "STEER: ...")]
    projector, projection = project(entries, inputs)
    assert [(d.input_key, d.basis, d.joined_running_turn) for d in projection.deliveries] == [
        ("first", MatchBasis.WIRE_ID, False),
        ("steer", MatchBasis.WIRE_ID, True),
    ]
    assert len(projection.turns_observed) == 1
    drafts = in_order(projection.drafts)
    kinds_seen = kinds(drafts)
    assert kinds_seen.index("inbound_chat", 1) == kinds_seen.index("code_output") + 1
    assert projector.undelivered() == ()
    assert sum(t.messages for t in projector.usage_by_turn().values()) == 3
    assert total(projector.usage_by_turn()) == result_usage("steer", 0)


def test_the_queued_command_timestamp_is_older_but_the_order_follows_the_file() -> None:
    raw = rows("steer")
    stamps = {r["uuid"]: r["timestamp"] for r in raw if "uuid" in r and "timestamp" in r}
    queued = next(r for r in raw if r.get("type") == "attachment" and "queued_command" in str(r))
    previous = raw[raw.index(queued) - 1]
    assert str(queued["timestamp"]) < str(stamps[previous["uuid"]])
    entries = entries_of("steer")
    clocks = [e.order.clock_ms for e in entries]
    assert clocks == sorted(clocks)


def test_parallel_tool_calls_of_one_message_pair_with_their_own_results() -> None:
    entries = entries_of("steer")
    calls = [e for e in entries if isinstance(e, ToolCall)]
    results = [e for e in entries if isinstance(e, ToolResult)]
    assert len(calls) == len(results) == 3
    assert [r.call_id for r in results] == [c.call_id for c in calls]
    projector, projection = project(entries, [])
    assert projector.unanswered_calls() == () and projector.unresolved_results() == ()
    outputs = [d for d in in_order(projection.drafts) if d.kind.value == "code_output"]
    assert [d.payload for d in outputs] == ["A", "B", "C"]


def test_interrupts_close_their_turns_and_the_half_answer_is_kept_unfinished() -> None:
    inputs = [sent(w, f"k{i}", "t") for i, w in enumerate((INT_ONE, INT_TWO, INT_THREE))]
    projector, projection = project(entries_of("interrupt"), inputs)
    assert [t.reason for t in projection.turns_closed] == [
        "interrupted",
        "interrupted",
        "completed",
    ]
    assert all(
        e.status is ToolStatus.ERROR for e in entries_of("interrupt") if isinstance(e, ToolResult)
    )
    assert len(projection.deliveries) == 3
    _check_interrupt_drafts(in_order(projection.drafts))
    tallies = list(projector.usage_by_turn().values())
    assert [t.reliable for t in tallies] == [True, False, True]
    assert tallies[0].usage == result_usage("interrupt", 0)
    assert tallies[2].usage == result_usage("interrupt", 2)


def _check_interrupt_drafts(drafts: list[TimelineDraft]) -> None:
    markers = [d.payload for d in drafts if d.kind.value == "system_marker"]
    assert markers == ["status:Turn interrupted"] * 2
    half = [d for d in drafts if d.kind.value == "agent_chat" and not d.complete]
    assert len(half) == 1 and half[0].payload.startswith("1. ")


def test_a_denied_call_gets_its_output_and_the_turn_completes() -> None:
    entries = entries_of("denied")
    projector, projection = project(entries, [sent(DENIED, "k", "t")])
    outputs = [d for d in projection.drafts if d.kind.value == "code_output"]
    assert [d.tool_status for d in outputs] == [ToolStatus.DENIED, ToolStatus.DENIED]
    assert all(d.payload.startswith("Claude requested permissions") for d in outputs)
    assert projector.unanswered_calls() == ()
    assert [t.reason for t in projection.turns_closed] == ["completed"]
    assert total(projector.usage_by_turn()) == result_usage("denied", 0)


def test_a_killed_process_loses_its_last_message_and_resume_appends_to_the_same_file() -> None:
    inputs = [sent(KILL_ONE, "a", "t"), sent(KILL_TWO, "b", "t"), sent(RESUMED, "c", "t")]
    killed_entries = entries_of("kill")
    projector, before = project(killed_entries, inputs)
    assert [d.input_key for d in before.deliveries] == ["a", "b"]
    assert not any(d.kind.value == "agent_chat" for d in before.drafts)
    resumed_entries = entries_of("resume")
    assert {e.key for e in killed_entries} <= {e.key for e in resumed_entries}
    after = projector.feed(resumed_entries)
    assert [d.input_key for d in after.deliveries] == ["c"]
    assert kinds(in_order(after.drafts)) == ["inbound_chat", "agent_chat"]
    assert in_order(after.drafts)[-1].payload == "RESUMED"
    whole_projector, whole = project(resumed_entries, inputs)
    assert {d.key for d in whole.drafts} == {d.key for d in (*before.drafts, *after.drafts)}
    assert whole_projector.undelivered() == ()


def test_rereading_the_same_rows_projects_nothing_twice() -> None:
    entries = entries_of("basic")
    projector, first = project(entries, [sent(BASIC, "k1", "x")])
    assert first.drafts
    assert projector.feed(entries) == type(first)()
    projector.register(sent(BASIC, "k1-again", "x"))
    assert projector.feed(entries_of("basic")).deliveries == ()


def _subagent_entries() -> tuple[list[AnyEntry], list[AnyEntry], list[AnyEntry]]:
    return (
        entries_of("subagents"),
        entries_of("subagents", SYNC_AGENT),
        entries_of("subagents", ASYNC_AGENT),
    )


def test_subagent_activity_nests_under_its_call_whatever_the_arrival_order() -> None:
    main, sync_sub, async_sub = _subagent_entries()
    links = {e.child_record: e.call_id for e in main if isinstance(e, SubagentLink)}
    assert links == {SYNC_AGENT: SYNC_CALL, ASYNC_AGENT: ASYNC_CALL}
    orders = {
        "main_first": [*main, *sync_sub, *async_sub],
        "subs_first": [*sync_sub, *async_sub, *main],
        "by_clock": sorted([*main, *sync_sub, *async_sub], key=lambda e: e.order),
    }
    outcomes: dict[str, dict[str, tuple[DraftKind, str | None, str | None, Order]]] = {}
    for name, entries in orders.items():
        projector, projection = project(entries, [sent(SUBS, "k", "t")])
        assert projector.unlinked_records() == () and projector.unanswered_calls() == ()
        outcomes[name] = {
            d.key: (d.kind, d.parent_tool_use_id, d.turn, d.order) for d in projection.drafts
        }
    assert outcomes["main_first"] == outcomes["subs_first"] == outcomes["by_clock"]
    drafts = outcomes["main_first"]
    nested = {k: v for k, v in drafts.items() if v[1] is not None}
    assert {v[1] for v in nested.values()} == {SYNC_CALL, ASYNC_CALL}
    assert {v[2] for v in nested.values()} <= {v[2] for v in drafts.values() if v[1] is None}
    assert sorted(v[0].value for v in nested.values()) == [
        "agent_code",
        "agent_code",
        "code_output",
        "code_output",
        "subagent_text",
        "subagent_text",
    ]


def test_subagent_output_continues_after_the_parent_turn_and_its_usage_stays_apart() -> None:
    main, sync_sub, async_sub = _subagent_entries()
    projector, projection = project([*main, *sync_sub, *async_sub], [sent(SUBS, "k", "t")])
    drafts = in_order(projection.drafts)
    launched = next(d for d in drafts if d.payload == "launched")
    late = [d for d in drafts if d.parent_tool_use_id == ASYNC_CALL]
    assert late and all(d.order > launched.order for d in late if d.kind.value != "agent_code")
    by_turn = projector.usage_by_turn()
    assert set(by_turn) == {t.turn for t in projection.turns_observed}
    first_turn = projection.turns_observed[0].turn
    assert by_turn[first_turn].usage == result_usage("subagents", 0)
    by_record = projector.usage_by_record()
    assert set(by_record) == {"main", SYNC_AGENT, ASYNC_AGENT}
    assert all(by_record[a].messages == 2 for a in (SYNC_AGENT, ASYNC_AGENT))


def test_cli_started_turns_come_from_task_notifications_and_are_answered_in_place() -> None:
    main, sync_sub, async_sub = _subagent_entries()
    _, projection = project([*main, *sync_sub, *async_sub], [sent(SUBS, "k", "t")])
    observed = projection.turns_observed
    assert [(t.origin, t.cause) for t in observed] == [
        (TurnOrigin.INPUT, None),
        (TurnOrigin.HARNESS, HarnessCause.BACKGROUND_TASK),
        (TurnOrigin.HARNESS, HarnessCause.BACKGROUND_TASK),
    ]
    assert [t.reason for t in projection.turns_closed] == ["completed"] * 3


def test_a_task_notification_is_a_marker_and_its_turn_is_answered_in_place() -> None:
    main, sync_sub, async_sub = _subagent_entries()
    _, projection = project([*main, *sync_sub, *async_sub], [sent(SUBS, "k", "t")])
    observed = projection.turns_observed
    notices = [d for d in in_order(projection.drafts) if d.kind.value == "system_marker"]
    assert len(notices) == 2
    assert notices[0].payload.startswith("Background task bqvf6fo84 completed: ")
    assert notices[0].tool_use_id == "toolu_01DPCQQVfKq1sk2WfivBRdFU"
    assert notices[0].source == HarnessCause.BACKGROUND_TASK.value
    for notice, turn in zip(notices, observed[1:], strict=True):
        assert notice.turn == turn.turn
        answer = [
            d for d in projection.drafts if d.turn == turn.turn and d.kind.value == "agent_chat"
        ]
        assert len(answer) == 1 and answer[0].order > notice.order


def test_background_launches_are_marked_as_background_results() -> None:
    statuses = {e.call_id: e.status for e in entries_of("subagents") if isinstance(e, ToolResult)}
    assert statuses["toolu_01DPCQQVfKq1sk2WfivBRdFU"] is ToolStatus.BACKGROUND
    assert statuses[ASYNC_CALL] is ToolStatus.BACKGROUND
    assert statuses[SYNC_CALL] is ToolStatus.OK


def test_compaction_boundary_and_the_slash_command_input() -> None:
    entries = entries_of("compact")
    inputs = [sent(SUBS, "k", "t"), sent(COMPACT, "c", "/compact")]
    _, projection = project(entries, inputs)
    drafts = in_order(projection.drafts)
    marker = [d for d in drafts if d.payload == "status:Context compacted"]
    assert len(marker) == 1
    assert [d.input_key for d in projection.deliveries] == ["k", "c"]
    assert drafts[-1].payload == "/compact" and drafts[-1].part_ref == "c-row"
    assert not any("local-command" in d.payload or "being continued" in d.payload for d in drafts)
    assert projection.anomalies == ()


def test_a_fork_projects_only_what_came_after_the_copied_history() -> None:
    parent_keys = {e.key for e in entries_of("basic")}
    fork = entries_of("fork")
    inputs = [sent(FORKED, "f", "Reply with exactly the word FORKED. No tools.")]
    _, tail = project(fork, inputs, known_keys=parent_keys)
    assert [d.input_key for d in tail.deliveries] == ["f"]
    assert kinds(in_order(tail.drafts)) == ["inbound_chat", "agent_chat"]
    assert in_order(tail.drafts)[-1].payload == "FORKED"
    _, everything = project(fork, [sent(BASIC, "p", "x"), *inputs])
    assert len(everything.drafts) > len(tail.drafts)


def test_unknown_types_are_skipped_and_reported_once_per_type() -> None:
    state = ParseState()
    synthetic = [
        {"type": "future-row", "uuid": "u1"},
        {"type": "future-row", "uuid": "u2"},
        {"type": "attachment", "uuid": "u3", "attachment": {"type": "future_attachment"}},
        {
            "type": "system",
            "uuid": "u4",
            "timestamp": "2026-11-12T09:00:00.000Z",
            "subtype": "future",
        },
    ]
    entries = parse_all(synthetic, state)[1]
    projector, projection = project(entries)
    assert projection.drafts == ()
    assert [(a.anomaly, a.detail) for a in projection.anomalies] == [
        (AnomalyKind.UNKNOWN_TYPE, "future-row"),
        (AnomalyKind.UNKNOWN_TYPE, "attachment/future_attachment"),
        (AnomalyKind.UNKNOWN_TYPE, "system/future"),
    ]
    assert projector.feed(parse_all(synthetic, state, first_line=10)[1]).anomalies == ()


def test_a_known_type_missing_a_needed_field_is_an_exception_not_a_timeline_item() -> None:
    damaged = [
        {"type": "user", "timestamp": "2026-11-12T09:00:00.000Z", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-12T09:00:00.000Z"},
        {"type": "user", "uuid": "u9", "timestamp": "not a time", "message": {"content": "hi"}},
        {
            "type": "attachment",
            "uuid": "q",
            "timestamp": "2026-11-12T09:00:00.000Z",
            "attachment": {"type": "queued_command", "prompt": "x"},
        },
    ]
    _, projection = project(parse_all(damaged)[1])
    assert projection.drafts == ()
    assert [(a.anomaly, a.detail) for a in projection.anomalies] == [
        (AnomalyKind.MISSING_FIELD, "uuid"),
        (AnomalyKind.MISSING_FIELD, "message"),
        (AnomalyKind.MISSING_FIELD, "timestamp"),
        (AnomalyKind.MISSING_FIELD, "attachment.source_uuid"),
    ]


def test_bookkeeping_synthetic_and_meta_rows_are_ignored_without_noise() -> None:
    for scenario in ("basic", "multiblock", "resume", "compact"):
        entries = entries_of(scenario)
        assert not [e for e in entries if type(e).__name__ == "Anomaly"]
    resume = entries_of("resume")
    assert "No response requested." not in {getattr(e, "text", "") for e in resume}
    assert not any(isinstance(e, Interrupt) for e in entries_of("basic"))


def test_the_cli_version_is_readable_from_a_row_for_telemetry() -> None:
    versions = {row_version(r) for r in rows("basic")}
    assert "2.1.322" in versions
    assert source("basic")["cli"] == "Claude Code 2.1.322"
    assert parse_row(ParseState(), {"type": "user"}, line=0)[1][0].key.startswith("claude:anomaly")
