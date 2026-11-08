"""Field-surface contracts of the env-decode helpers: communication style, helper port, delay lists and disabled adapters; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import pytest

from base import config

# --- agent_communication_style: enum ---


def _style_from_env(**env: str) -> str | None:
    from base.config.domains.agent.settings import AgentSettings

    return AgentSettings.from_environment(env).agent_communication_style


def test_communication_style_defaults_to_off() -> None:
    """No env at all leaves the None sentinel (unset -> per-model resolution),
    whose shared floor is 'off' by user ruling (2026-08-22), so the section is
    omitted unless explicitly enabled."""
    from base.lm.registry import DEFAULT_TUNING

    assert _style_from_env() is None
    assert DEFAULT_TUNING.agent_communication_style == "off"


@pytest.mark.parametrize("style", ["oriented", "concise", "silent", "off"])
def test_communication_style_accepts_each_member(style: str) -> None:
    assert _style_from_env(MEIVA_AGENT_COMMUNICATION_STYLE=style) == style


@pytest.mark.parametrize("bad", ["loud", "", "verbose", "2"])
def test_unknown_communication_style_fails_fast(bad: str) -> None:
    """A value outside the documented members reaches Literal validation and
    raises rather than being coerced."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _style_from_env(MEIVA_AGENT_COMMUNICATION_STYLE=bad)


def test_communication_style_is_a_per_agent_enum() -> None:
    """The field is overridable per agent (spawn config_overlay) and surfaces to
    the config panel as an enum with its three members."""
    from base.config import FIELD_INFOS

    field = FIELD_INFOS["agent_communication_style"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert extra["per_agent"] is True
    assert extra["writable"] is True
    assert extra["scope"] == "cluster-default"
    assert extra["restart_required"] == "agent"
    assert (
        config.field_alias_map()["agent_communication_style"] == "MEIVA_AGENT_COMMUNICATION_STYLE"
    )


def _helper_port_from_env(**env: str) -> int:
    from base.config.domains.services.settings import ServiceSettings

    return ServiceSettings.from_environment(env).permissions_helper_port


def test_permissions_helper_port_defaults_to_9223() -> None:
    assert _helper_port_from_env() == 9223


def test_permissions_helper_port_reads_new_key() -> None:
    assert _helper_port_from_env(MEIVA_PERMISSIONS_HELPER_PORT="18010") == 18010


def test_permissions_helper_serialization_alias_is_the_new_key() -> None:
    """The config panel writes the NEW key (serialization alias wins), so a PUT
    lands on the canonical name, never resurrecting the legacy one."""
    from base.config import field_alias

    assert field_alias("permissions_helper_port") == "MEIVA_PERMISSIONS_HELPER_PORT"
    assert field_alias("permissions_helper_enabled") == "MEIVA_PERMISSIONS_HELPER_ENABLED"
