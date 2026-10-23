# meiva:note
# Gateway test fixtures (registered by `tests/fixtures/path_scopes.py`).

from __future__ import annotations

from collections.abc import Generator
from typing import cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.config.tests.api_keys import mock_api_keys as mock_api_keys
from base.db import Database
from base.events.live.bus import EventBus
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import model_catalog as model_catalog
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.tests.support.runner_ops.local_runner import local_runner
from gateway.tests.support.runner_ops.ops_stub import RunnerOps

__all__ = ["gateway_local_runner", "mock_api_keys", "model_catalog"]


@pytest.fixture(autouse=True)
def gateway_local_runner(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> Generator[RunnerOps]:
    """The gateway's own machine, registered as a runner that runs its ops in this process.

    A spawn, lifecycle or config request the gateway forwards to its own machine reaches the
    real ops route over HTTP. A test that needs another machine, or scripted answers for this
    one, serves them with `runner_ops`; its registration replaces this one.
    """
    # meiva:note
    # the data plane's Redis, which the ops route and the gateway both publish to
    _ = event_bus
    catalog = (
        cast(tuple[str, ModelCatalog], request.getfixturevalue("withdrawn_model"))[1]
        if "withdrawn_model" in request.fixturenames
        else model_catalog
    )
    with local_runner(
        db_conn,
        database=database,
        authority=config_authority,
        catalog=catalog,
        machine=machine_identity,
        host=fake_host(),
        image=LOADED_IMAGE,
    ) as runner:
        yield runner
