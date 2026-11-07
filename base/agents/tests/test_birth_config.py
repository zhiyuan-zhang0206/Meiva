# meiva:note
# `base/agents/model/birth_config.py` — what a frozen field resolves to at the spawn boundary.
# The resolution chain proper (`config_overlay > birth_config > current config`) is
# enforced in two halves that meet nowhere else: this module decides WHAT gets
# stamped, and `agent/loop.py` decides in what ORDER the two stored maps are applied
# at boot. Both halves are covered here and in `tests/components/agent/test_loop_main.py`.

from __future__ import annotations

import pytest

from base.agents.model.birth_config import resolve_birth_config
from base.config import frozen_field_names, live_field_names
from base.config.service_read import ConfigAuthority


class TestResolveBirthConfig:
    def test_requires_explicit_model_when_uninherited(
        self, config_authority: ConfigAuthority
    ) -> None:
        with pytest.raises(ValueError, match="no model configured"):
            resolve_birth_config(authority=config_authority)

    def test_overlay_present_field_is_not_double_stamped(
        self, config_authority: ConfigAuthority
    ) -> None:
        """A field the spawner chose lives in config_overlay and outranks the stamp;
        copying it into birth_config too would erase the provenance the split
        exists for."""
        stamped = resolve_birth_config(
            {"llm_model": "claude-sonnet-5"},
            authority=config_authority,
        )
        assert "llm_model" not in stamped
        # Every other frozen field is still stamped.
        assert set(stamped) == frozen_field_names() - {"llm_model"}
        assert not set(stamped) & live_field_names()

    def test_inherited_model_is_stamped(self, config_authority: ConfigAuthority) -> None:
        stamped = resolve_birth_config(
            inherited={"llm_model": "claude-sonnet-5"},
            authority=config_authority,
        )
        assert stamped["llm_model"] == "claude-sonnet-5"
        assert set(stamped) == frozen_field_names()

    def test_a_live_field_in_the_overlay_changes_nothing(
        self, config_authority: ConfigAuthority
    ) -> None:
        stamped = resolve_birth_config(
            {"auto_compact_fraction": 0.5, "llm_model": "claude-sonnet-5"},
            authority=config_authority,
        )
        assert "llm_model" not in stamped
        assert set(stamped) == frozen_field_names() - {"llm_model"}

    def test_none_is_stamped_not_dropped(self, config_authority: ConfigAuthority) -> None:
        """For a per-model-defaultable field, None is the "the cluster expressed no
        opinion" sentinel. Freezing THAT is what keeps a later explicit cluster
        choice from reaching this agent, so the key must be present with a null."""
        stamped = resolve_birth_config(
            inherited={"llm_model": "claude-sonnet-5"},
            authority=config_authority,
        )
        assert "reasoning_effort" in stamped
        assert stamped["reasoning_effort"] is None


class TestForkInheritance:
    def test_parent_stamp_is_carried_over_verbatim(self, config_authority: ConfigAuthority) -> None:
        """A fork is the same identity continuing — it must not silently re-resolve
        its brain against today's defaults."""
        parent = dict.fromkeys(frozen_field_names(), "PARENT")
        assert resolve_birth_config(inherited=parent, authority=config_authority) == parent

    def test_a_frozen_field_the_parent_predates_is_resolved_fresh(
        self, config_authority: ConfigAuthority
    ) -> None:
        """An old agent's stamp does not cover a frozen field added since. The fork
        is still fully stamped rather than left with a hole that resolves live."""
        partial = {"llm_model": "claude-sonnet-5"}
        stamped = resolve_birth_config(inherited=partial, authority=config_authority)
        assert stamped["llm_model"] == "claude-sonnet-5"
        assert set(stamped) == frozen_field_names()

    def test_the_forks_own_overlay_still_outranks_the_inherited_stamp(
        self, config_authority: ConfigAuthority
    ) -> None:
        """The inherited value stays on the row (it is the parent's birth fact); the
        fork's explicit choice wins at apply time, not by erasing the stamp."""
        parent = {"llm_model": "claude-sonnet-5"}
        stamped = resolve_birth_config(
            {"llm_model": "deepseek-v4-flash"},
            inherited=parent,
            authority=config_authority,
        )
        assert stamped["llm_model"] == "claude-sonnet-5"
