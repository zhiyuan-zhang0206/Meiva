from __future__ import annotations

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.hooks.chunk_turn import Baseline, ClosingChunk, EnqueueChunk, plan_closing, plan_turn
from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import Chunk, segment_head_len

_THRESHOLD = 1000
# meiva:note
# A segment past its first turn: cut just after the head, 500 tokens of head.
_BASELINED = CompactState(understanding_cut_index=1, understanding_cut_tokens=500)


def _request(n: int) -> list[AnyMessage]:
    return [
        SystemMessage(content="head", id="h"),
        *(HumanMessage(content=str(i), id=f"m{i}") for i in range(1, n)),
    ]


def _reply(input_tokens: int) -> AIMessage:
    return AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 1,
            "total_tokens": input_tokens + 1,
        },
    )


def _framework(kind: str, id_: str) -> HumanMessage:
    return HumanMessage(
        content=f"<system_note>\n{id_}\n</system_note>",
        id=id_,
        additional_kwargs={"meiva_msg_type": kind},
    )


def _new_agent_segment() -> list[AnyMessage]:
    return [
        SystemMessage(content="prompt", id="sys"),
        _framework("system_note", "timeout"),
        _framework("system_note", "tz"),
        _framework("system_note", "agent-id"),
        _framework("system_note", "memory-index"),
        HumanMessage(
            content="first task", id="u1", additional_kwargs={"meiva_msg_type": "inbound"}
        ),
        AIMessage(content="ok", id="a1"),
    ]


def _compacted_segment() -> list[AnyMessage]:
    return [
        SystemMessage(content="prompt", id="sys"),
        _framework("system_note", "timeout"),
        _framework("system_note", "tz"),
        _framework("system_note", "agent-id"),
        _framework("system_note", "memory-index"),
        _framework("compact_summary", "summary"),
        _framework("system_note", "dump-path"),
        AIMessage(content="continuing", id="a1"),
        HumanMessage(content="later", id="u2", additional_kwargs={"meiva_msg_type": "inbound"}),
    ]


def test_turn_below_the_threshold_plans_nothing() -> None:
    assert plan_turn(_BASELINED, _request(10), _reply(1499), threshold=_THRESHOLD) is None


def test_a_turn_past_the_threshold_plans_the_chunk_and_the_moved_cut() -> None:
    compact = _BASELINED.model_copy(update={"version": 4})

    plan = plan_turn(compact, _request(10), _reply(1500), threshold=_THRESHOLD)

    assert plan == EnqueueChunk(
        Chunk(1, 10),
        "m9",
        compact.model_copy(
            update={"understanding_cut_index": 10, "understanding_cut_tokens": 1500}
        ),
    )


def test_the_next_chunk_needs_another_threshold_of_growth_past_the_new_cut() -> None:
    plan = plan_turn(_BASELINED, _request(10), _reply(1500), threshold=_THRESHOLD)
    assert isinstance(plan, EnqueueChunk)

    assert plan_turn(plan.moved, _request(14), _reply(2499), threshold=_THRESHOLD) is None
    assert isinstance(
        plan_turn(plan.moved, _request(14), _reply(2500), threshold=_THRESHOLD), EnqueueChunk
    )


def test_a_chunk_whose_last_message_has_no_id_is_planned_with_that_fact() -> None:
    request = [*_request(9), HumanMessage(content="no id")]

    plan = plan_turn(_BASELINED, request, _reply(1500), threshold=_THRESHOLD)

    assert isinstance(plan, EnqueueChunk) and plan.end_msg_id is None


def test_a_turn_without_usage_plans_nothing() -> None:
    assert plan_turn(CompactState(), _request(10), AIMessage(content="x"), threshold=1) is None


def test_a_segments_first_turn_only_records_the_baseline_past_the_head() -> None:
    plan = plan_turn(CompactState(), _request(2), _reply(30_000), threshold=_THRESHOLD)

    assert isinstance(plan, Baseline)
    assert (plan.state.understanding_cut_index, plan.state.understanding_cut_tokens) == (1, 30_000)


def test_the_first_turn_of_a_compacted_segment_skips_the_summary_and_notes() -> None:
    plan = plan_turn(
        CompactState(version=2), _compacted_segment(), _reply(40_000), threshold=_THRESHOLD
    )

    assert isinstance(plan, Baseline)
    assert plan.state.understanding_cut_index == 7
    assert plan.state.version == 2


def test_compaction_plans_the_remainder_from_the_last_cut() -> None:
    compact = CompactState(version=2, understanding_cut_index=6, understanding_cut_tokens=900)

    assert plan_closing(compact, _request(12)) == ClosingChunk(Chunk(6, 12), "m11")


def test_compaction_without_a_remainder_plans_nothing() -> None:
    assert plan_closing(CompactState(understanding_cut_index=12), _request(12)) is None
    assert plan_closing(CompactState(version=0), _new_agent_segment()[:5]) is None


def test_the_closing_chunk_stops_before_an_unanswered_tool_call() -> None:
    open_call = AIMessage(
        content="", id="call", tool_calls=[{"name": "execute_code", "args": {}, "id": "t1"}]
    )
    compact = CompactState(version=2, understanding_cut_index=3, understanding_cut_tokens=900)

    assert plan_closing(compact, [*_request(8), open_call]) == ClosingChunk(Chunk(3, 8), "m7")


def test_the_closing_chunk_of_a_compacted_segment_starts_past_the_summary() -> None:
    plan = plan_closing(CompactState(version=2), _compacted_segment())

    assert plan is not None and plan.chunk == Chunk(7, 9)


def test_compaction_resets_the_cut_with_the_version() -> None:
    nxt = CompactState(
        version=2, understanding_cut_index=40, understanding_cut_tokens=70_000, reminder_shown=True
    ).next_segment()

    assert (nxt.version, nxt.understanding_cut_index, nxt.understanding_cut_tokens) == (3, 0, 0)
    assert nxt.reminder_shown


def test_head_of_a_new_agent_ends_at_the_first_user_message() -> None:
    assert segment_head_len(_new_agent_segment()) == 5


def test_head_of_a_compacted_segment_includes_summary_and_dump_note() -> None:
    assert segment_head_len(_compacted_segment()) == 7


def test_head_stops_at_the_first_non_framework_message_and_edge_cases() -> None:
    assert segment_head_len([]) == 0
    assert segment_head_len(_request(3)) == 1
    later = [*_new_agent_segment(), _framework("system_note", "later")]
    assert segment_head_len(later) == 5
    request = _framework("compact_request", "req")
    assert segment_head_len([SystemMessage(content="p"), request]) == 2
