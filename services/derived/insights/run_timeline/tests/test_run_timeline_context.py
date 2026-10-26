from __future__ import annotations

from collections.abc import Iterator
from cProfile import Profile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import FullHistory, single_segment_history
from base.agents.history.hierarchy.units import (
    DisplayBlock,
    display_blocks,
    divide_units,
    read_times,
)
from base.agents.history.hierarchy.usage import MessageUsage
from base.agents.history.message_tokens import MessageTokens
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import Clock
from base.config import settings
from base.db import Database, create_agent
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import resolve_context_budget
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline import context, router
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.derive import segment_view
from services.derived.insights.run_timeline.schemas import RunTimelineContext
from services.derived.insights.run_timeline.tokens import BlockContext, BlockContexts

_TIMELINE_INPUTS = TimestampPolicy.from_clock(
    Clock.from_settings(), timestamps_enabled=settings.general.message_timestamps
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def stamp(minutes: int) -> dict[str, object]:
    return {"meiva_created_at": (T0 + timedelta(minutes=minutes)).isoformat()}


def ai(text: str, tokens: int, minutes: int) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1},
        additional_kwargs=stamp(minutes),
    )


def human(text: str, minutes: int) -> HumanMessage:
    return HumanMessage(
        content=text, additional_kwargs={"meiva_msg_type": "inbound", **stamp(minutes)}
    )


def two_sessions() -> HistoryView:
    first = SystemMessage(content="first prompt")
    messages: list[BaseMessage] = [
        first,
        human("ask one", 0),
        ai("reply one", 100, 1),
        human("ask two", 10),
        ai("reply two", 40, 11),
    ]
    read = read_times(messages, timeline_inputs=_TIMELINE_INPUTS)
    units = display_blocks(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS), messages, read)
    history = FullHistory(
        messages,
        (first, SystemMessage(content="second prompt")),
        (1, 3),
    )
    return HistoryView.of(history, units, MessageUsage(messages), read)


def test_requests_carry_their_session_and_send_time() -> None:
    requests = context.llm_requests(two_sessions())
    assert [(r.idx, r.session) for r in requests] == [(2, 0), (4, 1)]

    assert requests[0].ts == T0
    assert requests[1].ts == T0 + timedelta(minutes=10)


def with_output(msg: AIMessage, output: int) -> AIMessage:
    assert msg.usage_metadata is not None
    msg.usage_metadata = {
        "input_tokens": msg.usage_metadata["input_tokens"],
        "output_tokens": output,
        "total_tokens": msg.usage_metadata["input_tokens"] + output,
    }
    return msg


def three_requests_then_a_session() -> HistoryView:
    first = SystemMessage(content="first prompt")
    messages: list[BaseMessage] = [
        first,
        human("ask one", 0),
        with_output(ai("reply one", 100, 1), 5),
        human("ask two", 2),
        human("ask three", 3),
        with_output(ai("reply two", 160, 4), 7),
        human("ask four", 5),
        ai("reply three", 190, 6),
        human("ask five", 10),
        ai("reply four", 40, 11),
    ]
    read = read_times(messages, timeline_inputs=_TIMELINE_INPUTS)
    units = display_blocks(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS), messages, read)
    history = FullHistory(
        messages,
        (first, SystemMessage(content="second prompt")),
        (1, 8),
    )
    return HistoryView.of(history, units, MessageUsage(messages), read)


def contexts(view: HistoryView) -> list[tuple[DisplayBlock, BlockContext]]:
    book = BlockContexts(view)
    return [(block, book.of(block)) for block in view.units]


def test_the_context_before_a_reply_is_the_input_of_the_request_that_made_it() -> None:
    view = three_requests_then_a_session()
    book = BlockContexts(view)
    inputs = {2: 100, 5: 160, 7: 190, 9: 40}
    by_reply: dict[int, list[tuple[DisplayBlock, BlockContext]]] = {}
    for block, ctx in contexts(view):
        if block.kind in ("thinking", "text", "call"):
            by_reply.setdefault(block.i0, []).append((block, ctx))
    for idx, input_tokens in inputs.items():
        first, last = by_reply[idx][0], by_reply[idx][-1]
        tokens = book.tokens(first[0]).context_tokens or 0

        assert first[1].context_total == input_tokens + tokens

        assert last[1].context_total == input_tokens + (view.tokens[idx].context_tokens or 0)
        assert all(
            c.request is not None and c.request.input == input_tokens for _, c in by_reply[idx]
        )


def test_a_message_block_is_the_context_through_its_last_message() -> None:
    view = three_requests_then_a_session()
    totals = {
        block.i0: ctx.context_total for block, ctx in contexts(view) if block.kind == "inbound"
    }

    assert totals[3] == 100 + 5 + (view.tokens[3].context_tokens or 0)
    assert totals[4] == (totals[3] or 0) + (view.tokens[4].context_tokens or 0)


def test_the_context_total_starts_over_after_a_compaction() -> None:
    by_idx = {
        block.i0: ctx
        for block, ctx in contexts(three_requests_then_a_session())
        if block.kind == "inbound"
    }
    assert by_idx[8].session == 1

    assert by_idx[8].context_total == 40
    assert (by_idx[1].session, (by_idx[1].context_total or 0) > 0) == (0, True)


def test_only_a_replys_blocks_carry_its_request() -> None:
    for block, ctx in contexts(three_requests_then_a_session()):
        assert (ctx.request is not None) == (block.kind in ("thinking", "text", "call"))


@pytest.mark.parametrize("context_tokens", [60, 90])
def test_turn_splits_are_shared_without_changing_counts_or_context_progression(
    context_tokens: int,
) -> None:
    reply = AIMessage(
        content=[{"type": "thinking", "thinking": "aa"}, {"type": "text", "text": "bb"}],
        tool_calls=[{"name": "execute_code", "args": {"code": "cc"}, "id": "call"}],
        usage_metadata={"input_tokens": 100, "output_tokens": 60, "total_tokens": 160},
        additional_kwargs=stamp(1),
    )
    messages: list[BaseMessage] = [SystemMessage(content="prompt"), human("ask", 0), reply]
    read = read_times(messages, timeline_inputs=_TIMELINE_INPUTS)
    blocks = display_blocks(
        divide_units(messages, timeline_inputs=_TIMELINE_INPUTS), messages, read
    )
    view = HistoryView.of(single_segment_history(messages), blocks, MessageUsage(messages), read)

    record = MessageTokens(context_tokens, 60, "estimated")
    segment = view.segments[0]
    view = replace(
        view,
        tokens=[*view.tokens[:2], record],
        segments=(replace(segment, messages=(*segment.messages[:-1], record)),),
    )
    book = BlockContexts(view)
    turn = [block for block in blocks if block.i0 == 2]
    assert {block.kind for block in turn} == {"thinking", "text", "call"}
    share = context_tokens // 3
    totals = {"thinking": 100 + share, "text": 100 + 2 * share, "call": 100 + context_tokens}
    with Profile() as profile:
        for block in [*turn, *reversed(turn)]:
            tokens = book.tokens(block)
            assert (tokens.context_tokens, tokens.generation_tokens, tokens.estimated) == (
                share,
                20,
                True,
            )
            assert book.of(block).context_total == totals[block.kind]

    split_calls = sum(
        entry.callcount
        for entry in profile.getstats()
        if not isinstance(entry.code, str) and entry.code.co_name == "ai_message_parts"
    )
    assert split_calls == 2


def test_a_point_resolves_to_the_next_request_else_the_last() -> None:
    requests = context.llm_requests(two_sessions())
    for at, expected in zip((0, 2, 3, 4, 99), (2, 2, 4, 4, 4), strict=True):
        found = context.request_at(requests, at)
        assert found is not None and found.idx == expected
    assert context.request_at([], 0) is None


class Views:
    def __init__(self, view: HistoryView) -> None:
        self.view = view

    def get(self, _agent: int, _snapshot: str | None = None):
        return SimpleNamespace(sessions=len(self.view.history.segment_starts))

    def request(self, _cut: object, at: int):
        found = context.request_at(context.llm_requests(self.view), at)
        if found is None:
            raise HTTPException(404, "the agent has made no LLM request")
        history = self.view.history
        start = history.segment_starts[found.session]
        stop = (
            history.segment_starts[found.session + 1]
            if found.session + 1 < len(history.segment_starts)
            else len(history.messages)
        )
        head = history.segment_heads[found.session]
        raw = ([head] if head is not None else []) + history.messages[start:stop]
        local = single_segment_history(raw)
        view = segment_view(
            local, self.view.read[start - 1] if start else None, inputs=_TIMELINE_INPUTS
        )
        segment = SimpleNamespace(offset=start - local.segment_starts[0], session=found.session)
        return found.idx, found.ts, segment, view


# meiva:note
# A pool over the test's Postgres: the context route resolves the agent's model and overrides from
# its stored row, so the real breakdown function runs against real rows.
@pytest.fixture
def pool(database: Database) -> Iterator[ConnectionPool[Any]]:
    result = database.pool(min_size=1, max_size=2)
    try:
        yield result
    finally:
        result.close()


# meiva:note
# The context route over `view`, with the real breakdown function: `default_model` is the cluster
# default the app was configured with, `pool` holds the agent's stored row.
def call(
    view: HistoryView,
    at: int,
    catalog: ModelCatalog,
    pool: ConnectionPool[Any],
    *,
    default_model: str = "claude-opus-5",
    agent_id: int = 7,
) -> RunTimelineContext:
    app_state = SimpleNamespace(
        db_pool=pool,
        run_timeline_reader=Views(view),
        catalog=catalog,
        insights_config=InsightsConfig(
            run_timeline_message_text_max=20,
            default_model=default_model,
        ),
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=app_state)})
    return router.get_run_timeline_context(request, agent_id, at)


def test_the_context_is_the_breakdown_of_that_request_summed_from_its_messages(
    model_catalog: ModelCatalog, pool: ConnectionPool[Any]
) -> None:
    result = call(two_sessions(), 3, model_catalog, pool)
    assert (result.request, result.session, result.sessions) == (4, 1, 2)
    assert result.ts == T0 + timedelta(minutes=10)
    assert result.total_input_tokens == 40
    assert result.estimated is False
    kinds = {c.kind: c.tokens for c in result.categories}
    assert set(kinds) == {"system_prompt", "user_input"}
    assert sum(kinds.values()) == 40
    first = call(two_sessions(), 0, model_catalog, pool)
    assert (first.session, first.total_input_tokens) == (0, 100)


def test_an_agent_with_no_request_has_no_context(
    model_catalog: ModelCatalog, pool: ConnectionPool[Any]
) -> None:
    messages: list[BaseMessage] = [human("hello", 0)]
    read = read_times(messages, timeline_inputs=_TIMELINE_INPUTS)
    view = HistoryView.of(FullHistory(messages, (None,), (0,)), [], MessageUsage(messages), read)
    with pytest.raises(HTTPException) as caught:
        call(view, 0, model_catalog, pool)
    assert caught.value.status_code == 404


def _window(catalog: ModelCatalog, model: str) -> int:
    return resolve_context_budget(
        model, ModelOverrides.from_pins({}), catalog=catalog
    ).max_context_tokens


def test_the_window_follows_the_configured_default_model(
    model_catalog: ModelCatalog, pool: ConnectionPool[Any]
) -> None:
    large, small = "claude-opus-5", "claude-haiku-4-5-20251001"
    assert _window(model_catalog, large) != _window(model_catalog, small)

    assert call(two_sessions(), 3, model_catalog, pool, default_model=large).max_input_tokens == (
        _window(model_catalog, large)
    )
    assert call(two_sessions(), 3, model_catalog, pool, default_model=small).max_input_tokens == (
        _window(model_catalog, small)
    )


def test_an_agent_that_names_its_own_model_keeps_it_over_the_default(
    db_conn: psycopg.Connection, model_catalog: ModelCatalog, pool: ConnectionPool[Any]
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, config_overlay) VALUES (%s, 'idling', %s)"
        " ON CONFLICT (id) DO UPDATE SET config_overlay = EXCLUDED.config_overlay",
        (agent_id, Jsonb({"llm_model": "claude-haiku-4-5-20251001"})),
    )
    db_conn.commit()

    result = call(
        two_sessions(), 3, model_catalog, pool, default_model="claude-opus-5", agent_id=agent_id
    )

    assert result.max_input_tokens == _window(model_catalog, "claude-haiku-4-5-20251001")
