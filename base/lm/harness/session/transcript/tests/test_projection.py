from __future__ import annotations

from base.lm.harness.events import HarnessCause, TurnOrigin, Usage
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    InboundPart,
    MatchBasis,
    SentInput,
)
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnomalyKind,
    HumanInput,
    Interrupt,
    Notice,
    Order,
    Reasoning,
    SubagentLink,
    TokenUsage,
    ToolCall,
    ToolResult,
    TurnEnd,
)
from base.lm.harness.session.transcript.projection import (
    CALLS_LIMIT,
    HOLD_ENTRIES,
    TURNS_LIMIT,
    Projector,
)


def _order(clock: int, seq: int = 0) -> Order:
    return Order(clock, seq)


def _input(key: str, text: str) -> SentInput:
    return SentInput(
        key=key,
        wire_id=f"wire-{key}",
        text=text,
        parts=(InboundPart(ref=f"{key}-row", text=text),),
    )


def test_an_input_without_an_id_is_recognised_by_its_text_in_send_order() -> None:
    projector = Projector()
    projector.register(_input("a", "hello"))
    projector.register(_input("b", "hello"))
    entries = [
        HumanInput(key="h1", order=_order(1), turn="t1", wire_id=None, text=" hello\n"),
        HumanInput(key="h2", order=_order(2), turn="t2", wire_id=None, text="hello"),
        HumanInput(key="h3", order=_order(3), turn="t3", wire_id=None, text="hello"),
    ]
    projection = projector.feed(entries)
    assert [(d.input_key, d.basis) for d in projection.deliveries] == [
        ("a", MatchBasis.TEXT),
        ("b", MatchBasis.TEXT),
    ]
    assert projection.unmatched_inputs == ("h3",)
    assert projector.undelivered() == ()


def test_an_id_the_table_does_not_know_is_shown_as_unmatched_not_dropped() -> None:
    projection = Projector().feed(
        [HumanInput(key="h", order=_order(1), turn="t", wire_id="zzz", text="typed elsewhere")]
    )
    assert projection.unmatched_inputs == ("h",)
    assert [(d.kind, d.source, d.payload) for d in projection.drafts] == [
        (DraftKind.INBOUND_CHAT, "unmatched", "typed elsewhere")
    ]


def test_a_steer_that_started_its_own_turn_is_not_joined() -> None:
    projector = Projector()
    projector.register(_input("s", "later"))
    projection = projector.feed(
        [HumanInput(key="h", order=_order(5), turn="t2", wire_id="wire-s", text="later")]
    )
    (delivery,) = projection.deliveries
    assert not delivery.joined_running_turn and delivery.turn == "t2"
    assert [t.turn for t in projection.turns_observed] == ["t2"]


def test_a_result_before_its_call_waits_and_pairs() -> None:
    projector = Projector()
    result = ToolResult(
        key="r", order=_order(9), turn="t", call_id="c", content="out", duration_ms=40
    )
    call = ToolCall(key="c", order=_order(1), turn="t", call_id="c", name="Bash", tool_input={})
    assert projector.feed([result]).drafts == ()
    assert projector.unresolved_results() == ("c",)
    projection = projector.feed([call])
    assert [(d.kind, d.exec_ms) for d in projection.drafts] == [
        (DraftKind.AGENT_CODE, None),
        (DraftKind.CODE_OUTPUT, 40),
    ]
    assert projector.unresolved_results() == () and projector.unanswered_calls() == ()


def test_a_late_user_message_corrects_the_origin_of_a_turn_seen_first_through_its_answer() -> None:
    projector = Projector()
    projector.register(_input("k", "go"))
    first = projector.feed([AgentText(key="a", order=_order(9), turn="t", text="answer")])
    assert [(t.origin, t.cause) for t in first.turns_observed] == [
        (TurnOrigin.HARNESS, HarnessCause.UNKNOWN)
    ]
    second = projector.feed(
        [HumanInput(key="h", order=_order(1), turn="t", wire_id="wire-k", text="go")]
    )
    assert [(t.turn, t.origin) for t in second.turns_observed] == [("t", TurnOrigin.INPUT)]


def test_a_notice_that_opens_no_turn_leaves_the_current_turn_alone() -> None:
    projector = Projector()
    projection = projector.feed(
        [
            AgentText(key="a", order=_order(1), turn="t1", text="hi"),
            Notice(key="n", order=_order(2), text="sub-agent done", opens_turn=False),
            AgentText(key="b", order=_order(3), text="more"),
        ]
    )
    assert [t.turn for t in projection.turns_observed] == ["t1"]
    assert {d.turn for d in projection.drafts} == {"t1"}


def test_a_child_records_notice_becomes_nested_sub_agent_text_in_the_calls_turn() -> None:
    projector = Projector()
    entries = [
        Notice(key="n", order=_order(5), record="child", text="task finished"),
        ToolCall(key="c", order=_order(1), turn="t1", call_id="call", name="Agent", tool_input={}),
        SubagentLink(key="l", order=_order(2), turn="t1", call_id="call", child_record="child"),
    ]
    projection = projector.feed(entries)
    nested = [d for d in projection.drafts if d.record == "child"]
    assert [(d.kind, d.parent_tool_use_id, d.turn) for d in nested] == [
        (DraftKind.SUBAGENT_TEXT, "call", "t1")
    ]
    assert projector.unlinked_records() == ()


def test_reasoning_without_text_shows_nothing() -> None:
    projection = Projector().feed([Reasoning(key="r", order=_order(1), text="", duration_ms=5)])
    assert projection.drafts == ()


# meiva:note
# Nothing is held for ever and nothing grows without bound: a call whose turn closed is abandoned and no longer keeps
# the record from being quiet (its late result is still paired); a result whose call never comes, and the output of a
# sub-agent whose link never comes, are released top-level with an anomaly each, after a number of entries or when the
# writer says so; what the projector remembers has a fixed size however long the conversation.
def _call(key: str, call_id: str, seq: int) -> ToolCall:
    return ToolCall(key=key, order=_order(seq), call_id=call_id, name="Bash", tool_input={})


def test_a_call_unanswered_when_its_turn_closes_is_abandoned_and_a_late_result_still_pairs() -> (
    None
):
    projector = Projector()
    _ = projector.feed(
        [
            HumanInput(key="h", order=_order(0), turn="t1", wire_id=None, text="go"),
            _call("c", "call-1", 1),
            Interrupt(key="i", order=_order(2), turn="t1"),
        ]
    )
    assert projector.unanswered_calls() == (), "the interrupted call no longer blocks a quiet point"
    late = projector.feed(
        [ToolResult(key="r", order=_order(3), turn="t1", call_id="call-1", content="done")]
    )
    assert [d.kind for d in late.drafts] == [DraftKind.CODE_OUTPUT]


def test_a_call_left_open_by_a_turn_that_never_closed_is_abandoned_when_the_next_turn_begins() -> (
    None
):
    projector = Projector()
    _ = projector.feed(
        [
            HumanInput(key="h1", order=_order(0), turn="t1", wire_id=None, text="go"),
            _call("c", "call-1", 1),
        ]
    )
    assert projector.unanswered_calls() == ("call-1",)
    _ = projector.feed(
        [HumanInput(key="h2", order=_order(2), turn="t2", wire_id=None, text="more")]
    )
    assert projector.unanswered_calls() == ()


def test_a_result_whose_call_never_comes_is_released_top_level_after_the_bound() -> None:
    projector = Projector()
    first = projector.feed(
        [ToolResult(key="r", order=_order(0), turn="t", call_id="gone", content="late output")]
    )
    assert first.drafts == () and projector.unresolved_results() == ("gone",)
    filler = [
        AgentText(key=f"a{i}", order=_order(i + 1), turn="t", text="x", message_ref=f"m{i}")
        for i in range(HOLD_ENTRIES)
    ]
    released = projector.feed(filler)
    assert projector.unresolved_results() == ()
    assert [d.payload for d in released.drafts if d.kind is DraftKind.CODE_OUTPUT] == [
        "late output"
    ]
    assert [(a.anomaly, a.detail) for a in released.anomalies] == [
        (AnomalyKind.ORPHAN_RELEASED, "gone")
    ]


def test_the_output_of_a_sub_agent_that_is_never_linked_is_released_and_later_output_follows() -> (
    None
):
    projector = Projector()
    held = projector.feed(
        [AgentText(key="s1", order=_order(0), record="sub", text="one", message_ref="m1")]
    )
    assert held.drafts == () and projector.unlinked_records() == ("sub",)
    released = projector.feed([], release_held=True)
    assert [d.payload for d in released.drafts] == ["one"]
    assert [(a.anomaly, a.detail) for a in released.anomalies] == [
        (AnomalyKind.UNLINKED_RELEASED, "sub")
    ]
    later = projector.feed(
        [AgentText(key="s2", order=_order(1), record="sub", text="two", message_ref="m2")]
    )
    assert [(d.payload, d.parent_tool_use_id) for d in later.drafts] == [("two", None)]
    assert projector.unlinked_records() == ()


def test_what_the_projector_remembers_does_not_grow_with_the_conversation() -> None:
    projector = Projector()
    for turn in range(3_000):
        _ = projector.feed(
            [
                HumanInput(
                    key=f"h{turn}", order=_order(turn), turn=f"t{turn}", wire_id=None, text="q"
                ),
                _call(f"c{turn}", f"call-{turn}", turn),
                ToolResult(
                    key=f"r{turn}", order=_order(turn, 1), call_id=f"call-{turn}", content="ok"
                ),
                TokenUsage(
                    key=f"u{turn}",
                    order=_order(turn, 2),
                    turn=f"t{turn}",
                    message_ref=f"m{turn}",
                    usage=Usage(input_tokens=1, output_tokens=1),
                ),
                TurnEnd(key=f"e{turn}", order=_order(turn, 3), turn=f"t{turn}"),
            ]
        )
    sizes = projector.memory_sizes()
    assert max(sizes.values()) <= 20_000, sizes
    assert sizes["calls"] <= CALLS_LIMIT and sizes["turns"] <= TURNS_LIMIT


def test_the_keys_and_calls_the_projector_remembers_stop_at_their_bounds() -> None:
    projector = Projector(seen_limit=10, calls_limit=5)
    for n in range(100):
        _ = projector.feed(
            [
                ToolCall(
                    key=f"c{n}", order=Order(2 * n), call_id=f"call{n}", name="Bash", tool_input={}
                ),
                ToolResult(key=f"r{n}", order=Order(2 * n + 1), call_id=f"call{n}", content="ok"),
            ]
        )
        sizes = projector.memory_sizes()
        assert sizes["seen"] <= 10 and sizes["calls"] <= 5, (n, sizes)
    assert projector.memory_sizes()["seen"] == 10 and projector.memory_sizes()["calls"] == 5


def _held_text(record: str, n: int) -> AgentText:
    return AgentText(
        key=f"{record}-{n}", order=Order(n), record=record, text="x", message_ref=f"{record}m{n}"
    )


def test_a_record_that_alone_holds_its_bound_is_released_top_level_and_reported() -> None:
    projector = Projector(hold_entries=3)
    quiet = projector.feed([_held_text("subX", 0), _held_text("subX", 1)])
    assert quiet.drafts == () and projector.unlinked_records() == ("subX",)
    full = projector.feed([_held_text("subX", 2)])
    assert [d.parent_tool_use_id for d in full.drafts] == [None, None, None]
    assert [(a.anomaly, a.detail) for a in full.anomalies] == [
        (AnomalyKind.UNLINKED_RELEASED, "subX")
    ]
    assert projector.unlinked_records() == ()


def test_everything_held_together_has_a_bound_and_the_oldest_record_goes_first() -> None:
    projector = Projector(hold_entries=100, held_total_limit=4)
    projector.feed([_held_text("subA", 0), _held_text("subA", 1)])
    over = projector.feed([_held_text("subB", 0), _held_text("subB", 1), _held_text("subB", 2)])
    assert {d.record for d in over.drafts} == {"subA"}, (
        "five held against a bound of four: the oldest record is shown"
    )
    assert projector.unlinked_records() == ("subB",)


def test_a_closed_main_turn_releases_nothing_held_because_it_only_says_how_far_main_was_read() -> (
    None
):
    entries = [
        HumanInput(key="i", order=Order(0), turn="t0", wire_id=None, text="go"),
        _held_text("subX", 0),
        TurnEnd(key="e", order=Order(1), turn="t0"),
    ]
    projector = Projector()
    assert [d.record for d in projector.feed(entries).drafts] == ["main"]
    assert projector.unlinked_records() == ("subX",)
    assert [d.record for d in projector.feed([], release_held=True).drafts] == ["subX"]
