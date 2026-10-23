# meiva:note
# A Postgres owned by the test's home, started with native custody: the data plane a start prepares.
#
# The home is the test's own (`unit_home`); its cluster record is the real file a born home carries
# (`start-intent.json`), so the owner authority resolves its socket the way a start does.

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.lifecycle.start.migrations import cmd_migrations_apply
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime

__all__ = ["Plane", "free_port", "owned_pg", "plane", "write_record"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# meiva:note
# The cluster record a born home carries: its own ports, read back by `get_record`.
def write_record(home: Path, ports: Mapping[str, object]) -> None:
    record = {"ports": ports, "gateway_home": str(home), "created_at": "test"}
    (home / "start-intent.json").write_text(json.dumps({"record": record}))


@pytest.fixture
def owned_pg(
    postgres_access: None,
    unit_home: Path,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> Iterator[int]:
    """A home-owned Postgres (native launch receipt, owner-only socket) whose cluster record
    `local_owner_authority` resolves; yields its port."""
    pg_port = free_port()
    write_record(unit_home, {"postgres": pg_port})
    children: list[subprocess.Popen[bytes]] = []
    try:
        assert (
            ci.start_pg(
                data_plane_config,
                pg_port,
                retained_children=children,
                machine=command_runtime.machine,
            )
            == 0
        )
        yield pg_port
    finally:
        subprocess.run(
            [ci.pg_bin("pg_ctl"), "-D", str(unit_home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        for child in children:
            child.wait(timeout=1)


# meiva:note
# One home-owned Postgres and the values a start prepares it with.
@dataclasses.dataclass(frozen=True)
class Plane:
    home: Path
    port: int
    config: DataPlaneConfig
    database: OperatorDatabaseFactory
    command: CommandRuntime

    @property
    def data_dir(self) -> Path:
        return self.home / "pg"

    # meiva:note
    # The cluster URL names the owner and database as data, as a born `.env` does.
    def url(self, identity: str) -> str:
        return f"postgresql://{identity}@127.0.0.1:{self.port}/{identity}"

    def config_for(self, identity: str) -> DataPlaneConfig:
        return dataclasses.replace(
            self.config, db_url=self.url(identity), memory_search_backend="pgvector"
        )

    def admin_on(self, database: str) -> str:
        return make_conninfo(ci.pg_admin_url(self.port), dbname=database)

    def migrate(self, identity: str) -> list[str]:
        return cmd_migrations_apply(
            database_factory=self.database,
            machine=self.command.machine,
            config=self.config_for(identity),
            home=self.home,
        )


@pytest.fixture
def plane(
    owned_pg: int,
    unit_home: Path,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> Plane:
    return Plane(unit_home, owned_pg, data_plane_config, operator_database, command_runtime)
