from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.hooks import understanding_chunks as uc
from agent.state_channels import CompactState
from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunk_plan import plan_history, plan_replay
from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog

_THRESHOLD = 1000


def _read_agent(field: str) -> Any:
    if field == "understanding_enabled":
        return True
    if field == "understanding_chunk_ratio":
        return 1.0
    return getattr(settings.agent, field)


class _RecordingPool:
    def __init__(self, enqueued: list[tuple[int, int, str | None]]) -> None:
        self._enqueued = enqueued

    @asynccontextmanager
    async def connection(self, *, timeout: float | None = None) -> AsyncGenerator[Any, None]:
        del timeout
        yield self

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None, None]:
        yield

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[Any, None]:
        yield self

    async def execute(self, _sql: str, params: tuple[Any, ...] | None = None) -> None:
        if params is not None:
            self._enqueued.append((int(params[2]), int(params[3]), params[4]))


def _segment() -> list[AnyMessage]:
    msgs: list[AnyMessage] = [
        SystemMessage(content="head", id="h"),
        HumanMessage(content="note", id="n", additional_kwargs={"meiva_msg_type": "system_note"}),
    ]
    for i in range(40):
        msgs.append(HumanMessage(content=f"u{i}", id=f"u{i}"))
        tokens = 2000 + 130 * i
        msgs.append(
            AIMessage(
                content="x",
                id=f"a{i}",
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": 1,
                    "total_tokens": tokens + 1,
                },
            )
        )
    return msgs


async def test_live_chunks_match_the_hook_turn_by_turn(*, model_catalog: ModelCatalog) -> None:
    enqueued: list[tuple[int, int, str | None]] = []
    pool = cast("Any", _RecordingPool(enqueued))
    model = next(iter(model_catalog.models))
    overrides = ModelOverrides.from_pins(
        {
            "compact_reminder_fraction": 0.1,
            "auto_compact_fraction": 0.2,
            "auto_compact_ceiling_tokens": 2000,
        }
    )
    msgs = _segment()
    compact = CompactState()
    for i, msg in enumerate(msgs):
        if isinstance(msg, AIMessage):
            update = await uc.due_chunk_update(
                compact,
                msgs[:i],
                msg,
                pool=pool,
                agent_id=1,
                model=model,
                overrides=overrides,
                catalog=model_catalog,
                read_agent=_read_agent,
            )
            compact = update.get("compact", compact)

    planned = plan_replay(msgs, threshold=_THRESHOLD)
    live = [p for p in planned if not p.closing]
    assert len(live) > 3
    assert [(p.chunk.start_index, p.chunk.end_index, p.end_msg_id) for p in live] == enqueued


def test_the_remainder_closes_the_segment_and_an_open_tool_call_is_not_part_of_it() -> None:
    msgs = _segment()
    planned = plan_replay(msgs, threshold=10**9)
    assert [(p.chunk.start_index, p.chunk.end_index, p.closing) for p in planned] == [
        (2, len(msgs), True)
    ]
    msgs.append(AIMessage(content="", id="open", tool_calls=[{"name": "t", "args": {}, "id": "c"}]))
    assert plan_replay(msgs, threshold=10**9)[-1].chunk.end_index == len(msgs) - 1


def test_history_is_cut_segment_by_segment_with_boundary_closing_chunks() -> None:
    seg = _segment()
    head, body = cast(SystemMessage, seg[0]), seg[1:]
    history = FullHistory(
        [*body, *body, *body],
        (head, head, head),
        (0, len(body), 2 * len(body)),
    )
    planned = plan_history(history, ["b0", "b1"], threshold=_THRESHOLD)
    for k in range(3):
        mine = [p for p in planned if p.segment == k]
        assert mine and all(p.chunk.start_index >= 1 for p in mine)
        closing = [p for p in mine if p.closing]
        if k < 2:
            assert [p.boundary_checkpoint_id for p in closing] == [f"b{k}"]
        else:
            assert closing == []
    assert all(p.boundary_checkpoint_id is None for p in planned if not p.closing)
