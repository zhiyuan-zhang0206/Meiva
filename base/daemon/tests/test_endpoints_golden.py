from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from base.host.env.registry import health_port_env_aliases

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


class _Row(BaseModel):
    port: int
    pidfile: Path
    override: int | None
    legacy_pidfile: Path


def _rows(home: Path, overrides: Mapping[str, int]) -> dict[str, _Row]:
    aliases = health_port_env_aliases()
    result = subprocess.run(
        [sys.executable, "-m", "base.daemon.tests._endpoint_child"],
        env={
            "PATH": os.defpath,
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            **{aliases[name]: str(port) for name, port in overrides.items()},
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    rows = TypeAdapter(dict[str, _Row]).validate_json(result.stdout)
    assert set(rows) == set(_FIXED_PORTS)
    for name, row in rows.items():
        assert row.port == (_FIXED_PORTS[name] if row.override is None else row.override), name
        assert row.pidfile == row.legacy_pidfile, name
    return rows


def test_the_table_names_exactly_the_daemons_with_a_registered_port() -> None:
    assert set(health_port_env_aliases()) == set(_FIXED_PORTS)


def test_default_settings_give_the_fixed_ports_and_run_dir_pidfiles(tmp_path: Path) -> None:
    rows = _rows(tmp_path, {})
    assert {name: row.port for name, row in rows.items()} == _FIXED_PORTS


def test_overridden_settings_give_the_override_in_every_row(tmp_path: Path) -> None:
    overrides = {name: 19000 + index for index, name in enumerate(sorted(_FIXED_PORTS))}
    rows = _rows(tmp_path, overrides)
    assert {name: row.port for name, row in rows.items()} == overrides


def test_a_partial_override_moves_only_the_overridden_rows(tmp_path: Path) -> None:
    rows = _rows(tmp_path, {"ops": 18113, "agent_host": 18114})
    assert {name: row.port for name, row in rows.items()} == {
        **_FIXED_PORTS,
        "ops": 18113,
        "agent_host": 18114,
    }


def test_pidfiles_follow_the_home(tmp_path: Path) -> None:
    rows = _rows(tmp_path, {})
    assert {name: row.pidfile for name, row in rows.items()} == {
        name: tmp_path / "run" / f"{name}.pid" for name in _FIXED_PORTS
    }
