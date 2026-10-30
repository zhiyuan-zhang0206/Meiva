"""Invocation-owned op executors for daemon contracts."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.agents.upload_delivery.paths import MAX_AGENT_UPLOAD_BYTES, MAX_AGENT_UPLOAD_FILES
from base.cluster.machine import GatewayAccess, MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.db_authority.unit import UnitIdentity
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
from ops.upload_delivery import ReceiverConfig
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


# meiva:note
# The ops daemon's request path over the test's Postgres, Redis and home. Its upload receiver is
# built the way the daemon builds it, with a placeholder gateway credential and a closed local port
# as the gateway, which the upload tests replace with their stub source.
@pytest.fixture
def ops(
    db_url: str,
    redis_url: str,
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    machine_identity: MachineIdentityReader,
    unit_home: Path,
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
            home=unit_home,
            receiver=ReceiverConfig(
                unit=UnitIdentity(machine=machine_identity.name(), home=str(unit_home.resolve())),
                upload_dir_name="uploads",
                access=GatewayAccess(
                    gateway_url="http://127.0.0.1:1",
                    cluster_secret="ops-test-secret",  # noqa: S106
                    remote_plane=False,
                    machine_token=None,
                    launcher_profile=None,
                    gateway_home=False,
                ),
                quota_bytes=MAX_AGENT_UPLOAD_BYTES,
                quota_files=MAX_AGENT_UPLOAD_FILES,
            ),
        )
    finally:
        pool.close()


@pytest.fixture
def agent_id(
    redis_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> int:
    """A real agent row born on this test machine."""
    return spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
