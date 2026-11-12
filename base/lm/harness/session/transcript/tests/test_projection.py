from __future__ import annotations

from base.lm.harness.events import HarnessCause, TurnOrigin
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    InboundPart,
    MatchBasis,
    SentInput,
)
from base.lm.harness.session.transcript.entries import (
    AgentText,
    HumanInput,
    Notice,
    Order,
    Reasoning,
    SubagentLink,
    ToolCall,
    ToolResult,
)
from base.lm.harness.session.transcript.projection import Projector


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
