from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from services.agent_runner.page_server.tests.page_pty import HOST


@pytest.fixture
def page_server_identity(machine_identity: MachineIdentity) -> None:
    machine_identity.set(host=HOST)


@pytest.fixture
def sync_pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool]:
    from base.config import settings

    pool: ConnectionPool = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    yield pool
    pool.close()


@pytest.fixture
def agent_id(
    redis_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    return spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )


@pytest.fixture
def serve_dir(tmp_path: Path) -> Path:
    path = tmp_path / "site"
    path.mkdir()
    return path
