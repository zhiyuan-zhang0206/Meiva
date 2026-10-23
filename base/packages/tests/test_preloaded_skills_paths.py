"""The preloaded-skills setting: its default, its environment parsing and its per-agent overlay."""

from __future__ import annotations

from base.config import FIELD_INFOS, AgentSettings, per_agent_field_names


def test_field_default_is_empty() -> None:
    """The shipped default preloads nothing — opt-in per agent/preset."""
    factory = FIELD_INFOS["skills_to_expand_at_start"].default_factory
    assert factory is not None
    assert factory() == []  # type: ignore[call-arg]


def test_env_comma_string_parses() -> None:
    """The env form (a comma string) splits into stripped entries."""
    s = AgentSettings(MEIVA_SKILLS_TO_EXPAND_AT_START="ultra_speed, meiva_memory.consolidation")  # pyright: ignore[reportArgumentType]
    assert s.skills_to_expand_at_start == ["ultra_speed", "meiva_memory.consolidation"]


def test_field_is_per_agent_overridable() -> None:
    """A spawner must be able to overlay it onto one worker (like the index)."""
    assert "skills_to_expand_at_start" in per_agent_field_names()
