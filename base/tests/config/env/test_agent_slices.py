# meiva:note
# `AgentSlices.resolve`: an agent's pin wins for the fields it holds, every other field is the live
# cluster default.

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any, cast

import pytest

from base.config import ConfigBoot, config_registry, get_field, settings
from base.config.agent_pins import resolve_agent_config_pins
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentBrain, AgentSlices, ModelOverrides, agent_setting

# meiva:note
# The agent's pins and the plugin-config view ride the dataclass beside the slices.
_NOT_SLICES = {"pins", "plugin_pins", "_plugin_view", "_default_reader"}


def _slice_values(slices: AgentSlices) -> dict[str, object]:
    groups = {
        "brain": slices.brain,
        "prompt": slices.prompt,
        "memory": slices.memory,
        "skill_recall": slices.skill_recall,
        "history_dump": slices.history_dump,
        "sdk_reminders": slices.sdk_reminders,
        "llm_policy": slices.llm_policy,
        "overrides": slices.overrides,
        "sandbox": slices.sandbox,
        "kernel": slices.kernel,
        "harness_cli": slices.harness_cli,
    }
    assert set(groups) == {group.name for group in fields(slices)} - _NOT_SLICES
    return {name: value for group in groups.values() for name, value in asdict(group).items()}


def _slice_fields() -> list[str]:
    return list(_slice_values(AgentSlices.resolve(default_reader=defaults())))


def _read_slice(slices: AgentSlices, name: str) -> Any:
    values = _slice_values(slices)
    assert name in values, name
    return values[name]


def _expected(name: str, pins: dict[str, Any]) -> Any:
    # meiva:note
    # What the agent holding `pins` reads for `name`: its pin, else the live default.
    value = pins[name] if name in pins else get_field(name)
    return tuple(cast("list[Any]", value)) if isinstance(value, list) else value


def _pinned_value(name: str) -> Any:
    current = get_field(name)
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 7
    if isinstance(current, float):
        return current + 1.5
    if isinstance(current, list):
        return ["pinned", name]
    return f"pinned-{name}"


def test_every_slice_field_is_a_per_agent_setting() -> None:
    fields_by_name = config_registry.fields()
    for name in _slice_fields():
        extra = fields_by_name[name].info.json_schema_extra
        assert isinstance(extra, dict) and extra["per_agent"] is True, name


def test_unpinned_fields_read_the_live_cluster_defaults() -> None:
    slices = AgentSlices.resolve(default_reader=defaults())
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, {}), name


def test_explicit_brain_uses_its_owner_without_changing_other_defaults() -> None:
    first = AgentBrain("first-owner-model")
    second = AgentBrain("second-owner-model")
    first_slices = AgentSlices.resolve(brain=first, default_reader=defaults())
    second_slices = AgentSlices.resolve(brain=second, default_reader=defaults())
    assert first_slices.brain is first
    assert second_slices.brain is second
    for name in _slice_fields():
        if name != "llm_model":
            assert _read_slice(first_slices, name) == _expected(name, {}), name
            assert _read_slice(second_slices, name) == _expected(name, {}), name


def test_every_pinned_field_reads_its_pin() -> None:
    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins, default_reader=defaults())
    for name in _slice_fields():
        assert _read_slice(slices, name) == _expected(name, pins), name


@pytest.mark.parametrize("parity", [0, 1])
def test_a_partial_pin_map_pins_some_fields_and_leaves_the_rest_live(parity: int) -> None:
    names = _slice_fields()
    pins = {name: _pinned_value(name) for i, name in enumerate(names) if i % 2 == parity}
    slices = AgentSlices.resolve(pins, default_reader=defaults())
    for name in names:
        assert _read_slice(slices, name) == _expected(name, pins), name


def test_the_overlay_wins_over_the_birth_config() -> None:
    pins = resolve_agent_config_pins(
        config_overlay={"llm_model": "overlay-model"},
        birth_config={"llm_model": "birth-model", "memory_inherit_depth": 4},
    )
    slices = AgentSlices.resolve(pins, default_reader=defaults())
    assert slices.brain.llm_model == "overlay-model"
    assert slices.memory.memory_inherit_depth == 4


def test_a_live_default_edit_reaches_the_next_resolution() -> None:
    owner = ConfigBoot()
    owner.set_field("history_dump_keep", settings.agent.history_dump_keep)

    def read_owner(domain: str, name: str) -> Any:
        if domain == "agent" and name == "history_dump_keep":
            return owner.view.agent.history_dump_keep
        return defaults()(domain, name)

    before = AgentSlices.resolve(default_reader=read_owner)
    owner.set_field("history_dump_keep", before.history_dump.history_dump_keep + 3)
    after = AgentSlices.resolve(default_reader=read_owner)
    assert after.history_dump.history_dump_keep == before.history_dump.history_dump_keep + 3
    assert before.history_dump.history_dump_keep != after.history_dump.history_dump_keep


def test_list_settings_are_frozen_into_tuples() -> None:
    slices = AgentSlices.resolve({"sdk_disable": ["meiva.x", "meiva.y"]}, default_reader=defaults())
    assert slices.prompt.sdk_disable == ("meiva.x", "meiva.y")


def test_read_serves_a_setting_no_slice_names_from_the_pin_else_the_live_default() -> None:
    assert AgentSlices.resolve(default_reader=defaults()).read(
        "sandbox", "exec_node_timeout_seconds"
    ) == (settings.sandbox.exec_node_timeout_seconds)
    pinned = AgentSlices.resolve({"exec_node_timeout_seconds": 123.5}, default_reader=defaults())
    assert pinned.read("sandbox", "exec_node_timeout_seconds") == 123.5


# meiva:note
# The completion-notice policy is read by the gateway's notice delivery from the agent's stored
# overlay (`effective_completion_notice_policy`), not inside a turn.
_PER_AGENT_OUTSIDE_THE_SLICES = {"completion_notice_policy"}


def test_every_per_agent_setting_is_in_a_slice_or_read_from_the_stored_overlay() -> None:
    assert config_registry.per_agent_field_names() - set(_slice_fields()) == (
        _PER_AGENT_OUTSIDE_THE_SLICES
    )


def test_overrides_from_pins_reads_only_the_pins() -> None:
    overrides = ModelOverrides.from_pins({"auto_compact_fraction": 0.7, "unrelated": 1})
    assert overrides.auto_compact_fraction == 0.7
    assert overrides.reasoning_effort is None
    assert ModelOverrides.from_pins(None) == ModelOverrides.from_pins({})


def test_two_owner_defaults_keep_resolved_slices_frozen_and_unsliced_reads_live() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("llm_model", "first-model")
    second.set_field("llm_model", "second-model")
    first.set_field("history_dump_keep", 4)
    second.set_field("history_dump_keep", 8)
    first.set_field("exec_timeout_seconds", 101.0)
    second.set_field("exec_timeout_seconds", 202.0)
    first.set_field("exec_node_timeout_seconds", 501.0)
    second.set_field("exec_node_timeout_seconds", 602.0)

    def one(domain: str, field: str) -> Any:
        return first.get_field(field)

    def two(domain: str, field: str) -> Any:
        return second.get_field(field)

    a = AgentSlices.resolve(default_reader=one)
    b = AgentSlices.resolve(default_reader=two)
    pinned = AgentSlices.resolve(
        {"exec_timeout_seconds": 25.0, "exec_node_timeout_seconds": 703.0},
        default_reader=one,
    )
    assert (a.brain.llm_model, b.brain.llm_model) == ("first-model", "second-model")
    assert (a.history_dump.history_dump_keep, b.history_dump.history_dump_keep) == (4, 8)
    assert (a.sandbox.exec_timeout_seconds, b.sandbox.exec_timeout_seconds) == (101.0, 202.0)
    assert pinned.sandbox.exec_timeout_seconds == 25.0
    first.set_field("history_dump_keep", 12)
    first.set_field("exec_timeout_seconds", 404.0)
    first.set_field("exec_node_timeout_seconds", 804.0)
    assert a.history_dump.history_dump_keep == 4
    assert a.sandbox.exec_timeout_seconds == 101.0
    assert AgentSlices.resolve(default_reader=one).history_dump.history_dump_keep == 12
    assert AgentSlices.resolve(default_reader=one).sandbox.exec_timeout_seconds == 404.0
    assert a.read("sandbox", "exec_node_timeout_seconds") == 804.0
    assert b.read("sandbox", "exec_node_timeout_seconds") == 602.0
    assert pinned.read("sandbox", "exec_node_timeout_seconds") == 703.0
    assert agent_setting("exec_timeout_seconds", default_reader=one) == 404.0


def test_fully_pinned_reads_never_call_the_default_reader() -> None:
    def unexpected_default(domain: str, field: str) -> Any:
        raise AssertionError(f"unexpected default read: {domain}.{field}")

    pins = {name: _pinned_value(name) for name in _slice_fields()}
    slices = AgentSlices.resolve(pins, default_reader=unexpected_default)
    assert slices.brain.llm_model == pins["llm_model"]
    assert slices.read("lm", "llm_model") == pins["llm_model"]
    assert agent_setting("llm_model", pins, default_reader=unexpected_default) == pins["llm_model"]
