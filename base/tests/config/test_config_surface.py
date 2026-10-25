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


# --- delay-list env field (MEIVA_IM_SEND_RETRY_DELAYS; MEIVA_EMBED_RETRY_DELAYS
# removed in R2-D — the embedder's retry policy is a base.host.net.resilience Policy
# constant now, per design evaluation-record #14) ---


def _im_send_delays_from_env(**env: str) -> list[float]:
    from base.config.domains.services.settings import ServiceSettings

    return ServiceSettings.from_environment(env).im_send_retry_delays


def test_delay_lists_accept_comma_separated_env() -> None:
    """A comma-separated env value parses into the delay list — the spelling a
    .env operator would naturally write (task #698 G8; regression: NoDecode
    handed the raw string to list[float] and Settings construction crashed,
    killing every spawned agent in e2e)."""
    im = _im_send_delays_from_env(MEIVA_IM_SEND_RETRY_DELAYS="0.5,1")
    assert im == [0.5, 1.0]


def test_delay_lists_accept_json_array_env() -> None:
    """The JSON-array spelling pydantic-settings would natively decode also
    works."""
    im = _im_send_delays_from_env(MEIVA_IM_SEND_RETRY_DELAYS="[2.0, 4.0, 8.0]")
    assert im == [2.0, 4.0, 8.0]


def test_delay_lists_default_without_env() -> None:
    im = _im_send_delays_from_env()
    assert im == [2.0, 4.0, 8.0, 16.0, 32.0]


# --- MEIVA_IM_DISABLED_ADAPTERS (Task #855; P0 fix: NoDecode list field needs
# the same before-validator treatment or any env value crashes settings load) ---


def _disabled_adapters_from_env(value: str | None = None) -> list[str]:
    from base.config.domains.services.settings import ServiceSettings

    env = {} if value is None else {"MEIVA_IM_DISABLED_ADAPTERS": value}
    return ServiceSettings.from_environment(env).im_disabled_adapters


def test_disabled_adapters_accept_comma_separated_env() -> None:
    """The natural .env spelling — "weixin,feishu" — parses into the list."""
    assert _disabled_adapters_from_env("weixin,feishu") == ["weixin", "feishu"]


def test_disabled_adapters_accept_json_array_env() -> None:
    """The JSON-array spelling works too."""
    assert _disabled_adapters_from_env('["weixin", "feishu"]') == [
        "weixin",
        "feishu",
    ]


def test_disabled_adapters_accept_empty_env() -> None:
    """An empty value must not crash — it means "nothing disabled"."""
    assert _disabled_adapters_from_env("") == []


def test_disabled_adapters_default_without_env() -> None:
    """No env at all -> default empty (all adapters load)."""
    assert _disabled_adapters_from_env() == []
