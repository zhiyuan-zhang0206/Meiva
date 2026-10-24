"""Invocation-owned op executors for daemon contracts."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering
from services.agent_runner.agent_ops import boot
from services.agent_runner.agent_ops.tests.ops_support import Ops


@pytest.fixture
def op_executor(config_authority: ConfigAuthority) -> Iterator[ThreadPoolExecutor]:
    executor = boot.op_thread_pool(authority=config_authority)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True)


@pytest.fixture
def ops_database(database: Database) -> Callable[[], Database]:
    """The isolated test root's handle supplied to each actual dispatch consumer."""
    return lambda: database


@pytest.fixture
def ops_image(tmp_path: Path) -> LoadedCommit:
    """An explicit unknown image for routing cases that do not claim a commit."""
    return LoadedCommit(source_root=tmp_path, sha=None)


@pytest.fixture
def ops(
    db_url: str,
    redis_url: str,
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    machine_identity: MachineIdentity,
) -> Iterator[Ops]:
    """The ops daemon's request path over the test's Postgres, Redis and home."""
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=4, open=True)
    try:
        yield Ops(
            pool=pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            machine=machine_identity,
            host=fake_host(),
        )
    finally:
        pool.close()


@pytest.fixture
def agent_id(
    redis_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    """A real agent row born on this test machine."""
    return spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
