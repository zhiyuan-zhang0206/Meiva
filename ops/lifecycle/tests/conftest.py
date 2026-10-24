from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.api_keys import mock_api_keys as mock_api_keys
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from ops.lifecycle.tests.loopback.agent_host import ServedAgentHost, serve_agent_host


@pytest.fixture
def lifecycle_agent_id(
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> int:
    agent_id, _, _, _ = create_agent_row(
        database,
        event_bus,
        machine=machine_identity.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    return agent_id


@pytest.fixture
def db_pool(db_url: str) -> Iterator[ConnectionPool]:
    pool: ConnectionPool = ConnectionPool(
        db_url, min_size=1, max_size=1, kwargs={"prepare_threshold": None}
    )
    with pool:
        yield pool


@pytest.fixture
def agent_host(unit_home: Path) -> Iterator[ServedAgentHost]:
    with serve_agent_host(unit_home) as served:
        yield served
