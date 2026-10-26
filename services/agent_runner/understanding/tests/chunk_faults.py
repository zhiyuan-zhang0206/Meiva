# meiva:note
# The legacy chunk fault rig replaces precise generation and checkpoint boundaries;
# ordinary SDK and provider behavior is covered by test_provider_receipts.py.
import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunk_generate import ChunkResult
from base.agents.history.hierarchy.chunks import ChunkCall, LocatedChunk
from base.agents.history.hierarchy.leaf_groups import UnitGroup
from base.agents.history.hierarchy.units import divide_units
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.agent_runner.understanding import chunk_consumer as loop
from services.agent_runner.understanding.tests.consumer_helpers import consumer_config, lm_wiring
from services.agent_runner.understanding.tests.consumer_history import inbound_history


def chunk_result(
    located: LocatedChunk,
    *groups: UnitGroup,
    timeline_inputs: TimestampPolicy,
    summary: str = "what happened",
) -> ChunkResult:
    # meiva:note
    # A model's answer over `located`'s units: the given groups, else one group of everything.
    units = divide_units(list(located.messages), timeline_inputs=timeline_inputs)
    return ChunkResult(units, list(groups) or [UnitGroup(0, len(units) - 1, summary)])


@pytest.fixture
def chunk_timestamps() -> TimestampPolicy:
    # meiva:note
    # The fault rig's timestamp value, captured before its worker threads start.
    config = consumer_config()
    return TimestampPolicy.from_clock(
        Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        timestamps_enabled=config.view.general.message_timestamps,
    )


@pytest.fixture(autouse=True)
def chunk_seams(
    monkeypatch: pytest.MonkeyPatch, chunk_timestamps: TimestampPolicy
) -> dict[str, Any]:
    seen: dict[str, Any] = {"described": []}

    def segments(
        _db: Database, _agent_id: int, _boundary: str | None
    ) -> tuple[FullHistory, int | None]:
        if "segments_error" in seen:
            raise seen["segments_error"]
        return seen.get("history", inbound_history()), seen.get("closing_segment")

    monkeypatch.setattr(loop, "load_segments", segments)

    def describe(
        _models: object,
        _model: str,
        _overrides: object,
        located: LocatedChunk,
        _tools: object,
        calls: list[ChunkCall],
        _agent_id: int,
        **kw: object,
    ) -> ChunkResult:
        actual = kw["config"]
        assert isinstance(actual, ConfigBoot) and actual.is_full()
        assert (
            chunk_timestamps.timezone,
            chunk_timestamps.weekday,
            chunk_timestamps.timestamps_enabled,
        ) == (
            actual.view.general.timezone,
            actual.view.general.message_timestamp_weekday,
            actual.view.general.message_timestamps,
        )
        calls.extend(seen.get("calls", ()))
        if "error" in seen:
            raise seen["error"]
        seen["described"].append(located)
        seen["kw"] = kw
        return (
            seen["answer"](located)
            if "answer" in seen
            else chunk_result(located, timeline_inputs=chunk_timestamps)
        )

    monkeypatch.setattr(loop, "describe", describe)
    return seen


async def run_rounds(
    pool: AsyncConnectionPool,
    count: int,
    *,
    model_catalog: ModelCatalog,
    model: str = "deepseek-flash",
) -> None:
    # meiva:note
    # `count` times: claim at most one job and finish it (the loop's claim, one step at a time).
    config = consumer_config()
    config.set_field("llm_model", model)
    config.set_field("hierarchy_model", model)
    for _ in range(count):
        async with asyncio.TaskGroup() as tg:
            await loop.Consumer(
                pool, MagicMock(), [], **lm_wiring(model_catalog), config=config
            ).claim(tg)
