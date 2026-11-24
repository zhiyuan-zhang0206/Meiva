"""Field-surface contracts of the env-decode helpers: helper port, delay lists and disabled adapters; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations


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
