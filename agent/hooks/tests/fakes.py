from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langgraph.runtime import Runtime

from base.agents.context import MeivaContext
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.paths import resolve_meiva_home


def make_fake_ops_pool() -> None:
    return None


def placeholder_runtime(
    ops_pool: Any = None, *, database_gate: ProcessDbGate
) -> Runtime[MeivaContext]:
    ctx = MeivaContext(
        meiva_home=resolve_meiva_home(),
        ops_pool=ops_pool,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)
