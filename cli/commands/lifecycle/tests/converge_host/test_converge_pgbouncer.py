# meiva:note
# The pooler step keeps `MEIVA_DB_URL` on the port the pooler toggle names.
#
# The step's work is `converge_pgbouncer(config, home)`: the data-plane configuration is a value the
# test builds and the cluster record is the real `start-intent.json` of a home under `tmp_path`.

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot
from cli.commands.data_plane.pgbouncer import converge_pgbouncer
from cli.database.runtime import CommandRuntime

_DIRECT_URL = "postgresql://meiva_main:sek@127.0.0.1:5433/meiva_main"
_POOLED_URL = "postgresql://meiva_main:sek@127.0.0.1:6433/meiva_main"


# meiva:note
# The unit environment's data-plane configuration with the pooler toggled and local URLs.
def _plane(command_runtime: CommandRuntime, *, pooler: bool) -> DataPlaneConfig:
    return replace(
        data_plane_config_from_boot(command_runtime.config),
        pgbouncer_enabled=pooler,
        db_url=_DIRECT_URL,
        redis_url="redis://127.0.0.1:6380/0",
    )


# meiva:note
# A home that was born with the pg port 5433 and the pooler port 6433, and its `.env`.
def _born_home(home: Path, *, db_url: str | None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    ports = {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433}
    record = {"ports": ports, "gateway_home": str(home), "created_at": "t"}
    (home / "start-intent.json").write_text(json.dumps({"record": record}))
    if db_url is not None:
        (home / ".env").write_text(f"MEIVA_DB_URL={db_url}\n")
    return home


def test_enablement_migrates_a_direct_url_to_the_pooler(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    home = _born_home(tmp_path / "home", db_url=_DIRECT_URL)

    converge_pgbouncer(_plane(command_runtime, pooler=True), home)

    # meiva:note
    # the record's pooler 6433
    assert "MEIVA_DB_URL=" + _POOLED_URL in (home / ".env").read_text()


# meiva:note
# Remote data planes retain their provider URL and skip local binary preflight (#1752).
#
# The pooler toggle is meaningless for a remote plane, and the URL's port is the provider's.
def test_remote_data_planes_keep_the_provider_url_byte_identical(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    remote_url = "postgresql://meiva:sek@10.9.8.7:5432/meiva"
    home = _born_home(tmp_path / "home", db_url=remote_url)
    remote = replace(
        _plane(command_runtime, pooler=True),
        db_url=remote_url,
        redis_url="rediss://10.9.8.7:6380/0",
    )

    converge_pgbouncer(remote, home)

    assert "MEIVA_DB_URL=" + remote_url in (home / ".env").read_text()


# meiva:note
# Toggle off + restart -> the pooler never starts and the URL is rewritten to the pg port.
def test_the_kill_switch_rewrites_the_pooler_url_back_to_the_direct_port(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    home = _born_home(tmp_path / "home", db_url=_POOLED_URL)

    converge_pgbouncer(_plane(command_runtime, pooler=False), home)

    assert "MEIVA_DB_URL=" + _DIRECT_URL in (home / ".env").read_text()


# meiva:note
# No snapshot churn every start.
def test_a_url_that_already_matches_the_toggle_is_not_rewritten(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    home = _born_home(tmp_path / "home", db_url=_POOLED_URL)

    converge_pgbouncer(_plane(command_runtime, pooler=True), home)

    assert "MEIVA_DB_URL=" + _POOLED_URL in (home / ".env").read_text()


# meiva:note
# Converge only normalizes the two cluster ports.
def test_a_dev_stand_in_naming_neither_cluster_port_is_left_alone(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    stand_in = "postgresql://meiva:dev@localhost:5432/meiva"
    home = _born_home(tmp_path / "home", db_url=stand_in)

    converge_pgbouncer(_plane(command_runtime, pooler=True), home)

    assert f"MEIVA_DB_URL={stand_in}" in (home / ".env").read_text()


# meiva:note
# A fresh home converges before birth materializes URLs.
def test_a_home_without_an_env_file_is_a_no_op_not_a_crash(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    home = _born_home(tmp_path / "home", db_url=None)

    converge_pgbouncer(_plane(command_runtime, pooler=False), home)

    assert not (home / ".env").exists()


def test_a_home_with_no_cluster_record_is_left_untouched(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(f"MEIVA_DB_URL={_POOLED_URL}\n")

    converge_pgbouncer(_plane(command_runtime, pooler=False), home)

    assert (home / ".env").read_text() == f"MEIVA_DB_URL={_POOLED_URL}\n"
