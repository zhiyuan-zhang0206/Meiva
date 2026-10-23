from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from base.daemon.endpoints import ServiceEndpoints
from base.host.env.registry import health_port_env_aliases
from base.paths import meiva_home, run_dir

_FIXED_PORTS: Mapping[str, int] = {
    "labeler": 8103,
    "memory_indexer": 8105,
    "heartbeat": 8107,
    "task_maintenance": 8108,
    "events_maintenance": 8109,
    "delivery_watchdog": 8110,
    "im_bridge": 8111,
    "page_server": 8112,
    "ops": 8113,
    "agent_host": 8114,
    "pg_backup": 8116,
    "ttl_reaper": 8121,
    "schedule_manager": 8122,
}


def _old_health_port(name: str, read_field: Callable[[str], int | None]) -> int:
    override = read_field(f"{name}_health_port")
    return _FIXED_PORTS[name] if override is None else int(override)


def _old_pid_path(name: str) -> Path:
    return run_dir(meiva_home()) / f"{name}.pid"


def _assert_rows_match_the_old_lookups(read_field: Callable[[str], int | None]) -> None:
    table = ServiceEndpoints.from_settings(read_field=read_field)
    assert {endpoint.name for endpoint in table} == set(_FIXED_PORTS)
    for endpoint in table:
        assert endpoint.health_port == _old_health_port(endpoint.name, read_field), endpoint.name
        assert endpoint.pidfile == _old_pid_path(endpoint.name), endpoint.name


def test_the_table_names_exactly_the_daemons_with_a_registered_port() -> None:
    assert set(health_port_env_aliases()) == set(_FIXED_PORTS)


def test_default_settings_give_the_fixed_ports_and_run_dir_pidfiles() -> None:
    def read_field(_key: str) -> int | None:
        return None

    table = ServiceEndpoints.from_settings(read_field=read_field)
    assert {e.name: e.health_port for e in table} == _FIXED_PORTS
    _assert_rows_match_the_old_lookups(read_field)


def test_overridden_settings_give_the_override_in_every_row() -> None:
    overrides = {name: 19000 + index for index, name in enumerate(sorted(_FIXED_PORTS))}

    def read_field(key: str) -> int | None:
        return overrides.get(key.removesuffix("_health_port"))

    table = ServiceEndpoints.from_settings(read_field=read_field)
    assert {e.name: e.health_port for e in table} == overrides
    _assert_rows_match_the_old_lookups(read_field)


def test_a_partial_override_moves_only_the_overridden_rows() -> None:
    partial = {"ops_health_port": 18113, "agent_host_health_port": 18114}

    def read_field(key: str) -> int | None:
        return partial.get(key)

    table = ServiceEndpoints.from_settings(read_field=read_field)
    assert {e.name: e.health_port for e in table} == {
        **_FIXED_PORTS,
        "ops": 18113,
        "agent_host": 18114,
    }
    _assert_rows_match_the_old_lookups(read_field)


def test_pidfiles_follow_the_home(tmp_path: Path) -> None:
    table = ServiceEndpoints.from_settings(home=tmp_path)
    assert {e.name: e.pidfile for e in table} == {
        name: tmp_path / "run" / f"{name}.pid" for name in _FIXED_PORTS
    }
