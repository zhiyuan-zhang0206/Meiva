from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from agent.graph.hooks.compact import auto_compact_will_fire, compact_reminder_update
from agent.state import AgentState, CompactState
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY
from base.paths import meiva_home

build_model_catalog(home=meiva_home())

_MODEL = "deepseek-flash"
_TINY_FRACTION = 1e-6
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _state() -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
    ]
    return AgentState(messages=messages)


def _agent(**pins: float) -> AgentSlices:
    return AgentSlices.resolve(
        {"llm_model": _MODEL, **pins},
        default_reader=defaults(),
    )


def test_an_unpinned_agent_is_below_its_models_thresholds() -> None:
    assert not auto_compact_will_fire(
        _state(), _agent(), catalog=build_model_catalog(home=meiva_home())
    )
    assert (
        compact_reminder_update(
            _state(),
            _agent(),
            catalog=build_model_catalog(home=meiva_home()),
            extensions=EMPTY,
            now=_NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    "pin", [{"auto_compact_fraction": _TINY_FRACTION}, {"auto_compact_ceiling_tokens": 1}]
)
def test_an_agent_threshold_that_sits_below_the_conversation_forces_compaction(
    pin: dict[str, float],
) -> None:
    assert auto_compact_will_fire(
        _state(), _agent(**pin), catalog=build_model_catalog(home=meiva_home())
    )


def test_a_raised_force_threshold_is_what_keeps_a_long_conversation_going() -> None:
    state = _state()
    tiny = _agent(auto_compact_fraction=_TINY_FRACTION)
    assert auto_compact_will_fire(state, tiny, catalog=build_model_catalog(home=meiva_home()))
    assert not auto_compact_will_fire(
        state, _agent(auto_compact_fraction=0.9), catalog=build_model_catalog(home=meiva_home())
    )


def test_an_agent_reminder_threshold_below_the_conversation_injects_the_reminder() -> None:
    update: dict[str, Any] | None = compact_reminder_update(
        _state(),
        _agent(compact_reminder_fraction=_TINY_FRACTION),
        catalog=build_model_catalog(home=meiva_home()),
        extensions=EMPTY,
        now=_NOW,
    )
    assert update is not None
    compact: CompactState = update["compact"]
    assert compact.reminder_shown
