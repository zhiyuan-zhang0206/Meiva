from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph.hooks.compact import build_compact_transition, retained_tail
from agent.hooks.compact_anchor import compose_summary_message
from agent.hooks.history_dump import history_dump_note
from agent.messages import NoteTag, system_note_message
from base.lm.context_budget import latest_input_tokens


def _call(call_id: str, code: str = "print(1)") -> dict[str, object]:
    return {"name": "execute_code", "args": {"code": code}, "id": call_id, "type": "tool_call"}


def test_retained_tail_keeps_nothing_at_zero_or_below() -> None:
    messages = [HumanMessage(content="a"), AIMessage(content="b")]
    assert retained_tail(messages, 0) == []
    assert retained_tail(messages, -3) == []


def test_retained_tail_never_copies_the_standing_system_prompt() -> None:
    messages = [SystemMessage(content="<sys>"), HumanMessage(content="a"), AIMessage(content="b")]

    assert retained_tail(messages, 3) == messages[1:]


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


def test_retained_tail_drops_the_halt_pair_even_when_the_call_is_aliased() -> None:
    messages = [
        HumanMessage(content="request"),
        AIMessage(content="done"),
        AIMessage(content="", tool_calls=[_call("c9", "getattr(meiva.self, 'compact')('x')")]),
        ToolMessage(content="[system halt] You just called meiva.self.compact", tool_call_id="c9"),
    ]

    assert retained_tail(messages, 3) == messages[1:2]


def test_retained_tail_drops_a_compact_call_whose_halt_result_is_outside_the_window() -> None:
    messages = [
        HumanMessage(content="request"),
        AIMessage(content="winding down", tool_calls=[_call("c9", "meiva.self.compact('x')")]),
        ToolMessage(content="[system halt] You just called meiva.self.compact", tool_call_id="c9"),
        HumanMessage(content="a message that arrived later"),
    ]

    assert retained_tail(messages, 2) == messages[3:]


def test_retained_tail_strips_usage_recorded_before_the_replacement() -> None:
    prior = AIMessage(
        content="thinking",
        usage_metadata={"input_tokens": 900_000, "output_tokens": 10, "total_tokens": 900_010},
    )
    kept = retained_tail([HumanMessage(content="ask"), prior], 2)

    assert [message.content for message in kept] == ["ask", "thinking"]
    retained_ai = kept[1]
    assert isinstance(retained_ai, AIMessage)
    assert retained_ai.usage_metadata is None
    assert latest_input_tokens(kept) is None


def test_compact_transition_parks_real_notes_ahead_of_the_retained_tail(
    tmp_path: Path,
) -> None:
    marker = system_note_message(content="a lifecycle marker", tag=NoteTag.LIFECYCLE_RESTART)
    dump = history_dump_note(tmp_path / "history.jsonl")
    replaced = [HumanMessage(content="old"), AIMessage(content="old reply")]

    transition = build_compact_transition(
        "summary",
        resume="claim",
        extra_msgs=[marker, dump],
        discarded=replaced,
        keep_tail=2,
    )

    tail = transition["context_reset"].tail
    assert [message.content for message in tail] == [
        compose_summary_message("summary"),
        marker.content,
        dump.content,
        "old",
        "old reply",
    ]


def test_compact_transition_without_retention_parks_the_summary_only() -> None:
    transition = build_compact_transition(
        "summary", resume="claim", discarded=[HumanMessage(content="x")]
    )

    assert [message.content for message in transition["context_reset"].tail] == [
        compose_summary_message("summary")
    ]
