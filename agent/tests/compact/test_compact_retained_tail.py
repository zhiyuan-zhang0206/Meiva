from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph.hooks.compact import build_compact_transition, retained_tail
from agent.hooks.compact_anchor import compose_summary_message


def _call(call_id: str, code: str = "print(1)") -> dict[str, object]:
    return {"name": "execute_code", "args": {"code": code}, "id": call_id, "type": "tool_call"}


def test_retained_tail_keeps_nothing_at_zero_or_below() -> None:
    messages = [HumanMessage(content="a"), AIMessage(content="b")]
    assert retained_tail(messages, 0) == []
    assert retained_tail(messages, -3) == []


def test_retained_tail_drops_a_leading_tool_result() -> None:
    messages = [
        HumanMessage(content="request"),
        AIMessage(content="", tool_calls=[_call("c1")]),
        ToolMessage(content="result", tool_call_id="c1"),
        AIMessage(content="done"),
    ]

    assert retained_tail(messages, 2) == [messages[-1]]


def test_retained_tail_drops_a_tool_call_missing_one_result() -> None:
    messages = [
        HumanMessage(content="request"),
        AIMessage(content="", tool_calls=[_call("c1"), _call("c2")]),
        ToolMessage(content="r1", tool_call_id="c1"),
        AIMessage(content="still working"),
    ]

    assert retained_tail(messages, 3) == [messages[-1]]


def test_retained_tail_keeps_a_complete_tool_pair() -> None:
    messages = [
        HumanMessage(content="ask"),
        AIMessage(content="", tool_calls=[_call("c1")]),
        ToolMessage(content="result", tool_call_id="c1"),
    ]

    assert retained_tail(messages, 3) == messages


def test_retained_tail_drops_the_compaction_request_and_its_halt_result() -> None:
    messages = [
        HumanMessage(content="request"),
        AIMessage(content="progress"),
        AIMessage(content="winding down", tool_calls=[_call("c9", "meiva.self.compact('x')")]),
        ToolMessage(content="[system halt] You just called meiva.self.compact", tool_call_id="c9"),
    ]

    assert retained_tail(messages, 4) == messages[:2]
    assert retained_tail(messages, 2) == []


def test_compact_transition_parks_retained_tail_before_a_new_inbound() -> None:
    note = SystemMessage(content="history dumped")
    inbound = HumanMessage(content="new message")
    replaced = [HumanMessage(content="old"), AIMessage(content="old reply")]

    transition = build_compact_transition(
        "summary",
        resume="claim",
        extra_msgs=[note, inbound],
        discarded=replaced,
        keep_tail=2,
    )

    tail = transition["context_reset"].tail
    assert [m.content for m in tail] == [
        compose_summary_message("summary"),
        note.content,
        "old",
        "old reply",
        inbound.content,
    ]


def test_compact_transition_without_retention_parks_the_summary_only() -> None:
    transition = build_compact_transition(
        "summary", resume="claim", discarded=[HumanMessage(content="x")]
    )

    assert [m.content for m in transition["context_reset"].tail] == [
        compose_summary_message("summary")
    ]
