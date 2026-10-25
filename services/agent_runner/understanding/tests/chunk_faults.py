# meiva:note
# The legacy chunk fault rig replaces precise generation and checkpoint boundaries;
# ordinary SDK and provider behavior is covered by test_provider_receipts.py.
from typing import Any

import pytest

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.chunk_generate import ChunkResult
from base.agents.history.hierarchy.chunks import ChunkCall, LocatedChunk
from base.agents.history.hierarchy.leaf_groups import UnitGroup
from base.agents.history.hierarchy.units import divide_units
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.db import Database
from base.host.env.agent_slices import ModelOverrides
from services.agent_runner.understanding import chunk_consumer as loop
from services.agent_runner.understanding.tests.consumer_helpers import consumer_config
from services.agent_runner.understanding.tests.consumer_history import inbound_history


def chunk_result(
    located: LocatedChunk, *groups: UnitGroup, summary: str = "what happened"
) -> ChunkResult:
    # meiva:note
    # A model's answer over `located`'s units: the given groups, else one group of everything.
    config = consumer_config()
    units = divide_units(
        list(located.messages),
        timeline_inputs=TimestampPolicy.from_clock(
            Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
            timestamps_enabled=config.view.general.message_timestamps,
        ),
    )
    return ChunkResult(units, list(groups) or [UnitGroup(0, len(units) - 1, summary)])


@pytest.fixture(autouse=True)
def chunk_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"described": []}

    def target(*_args: object, **_kwargs: object) -> tuple[str, ModelOverrides]:
        return "deepseek-flash", ModelOverrides.from_pins(None)

    monkeypatch.setattr(loop, "agent_model_target", target)

    def segments(_db: Database, _agent_id: int, _boundary: str | None) -> tuple[FullHistory, None]:
        return seen.get("history", inbound_history()), None

    monkeypatch.setattr(loop, "load_segments", segments)

    def describe(
        _models: object,
        _model: str,
        _overrides: object,
        located: LocatedChunk,
        _tools: object,
        _calls: list[ChunkCall],
        _agent_id: int,
        **kw: object,
    ) -> ChunkResult:
        seen["described"].append(located)
        seen["kw"] = kw
        return seen["answer"](located) if "answer" in seen else chunk_result(located)

    monkeypatch.setattr(loop, "describe", describe)
    return seen
