# meiva:note
# Assembled gateway clients, bound only to HTTP and SDK gateway consumers.

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_app

__all__ = ["gateway_unit", "sdk_via_gateway"]


@pytest.fixture
def gateway_unit(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    machine_identity: ConfiguredMachineIdentity,
) -> Iterator[TestClient]:
    """The gateway unit (gateway): owns the test DB/config, runs the
    in-process gateway app, role='gateway'.
    """
    from base import paths
    from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess

    config = snapshot_process_config()
    config.boot()
    machine_identity.set(role="gateway")
    process = GatewayProcess(
        home=paths.meiva_home(),
        config=config,
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    app = gateway_test_app(process=process)
    _ = db_conn
    with (
        process.lifetime(),
        GatewayTestClient(app, base_url="http://test-gateway") as client,
    ):
        yield client


@pytest.fixture
def sdk_via_gateway(gateway_unit: TestClient) -> Iterator[TestClient]:
    """The gateway unit with the SDK's gateway client pointed at it: `meiva.agents.*` calls land
    in the in-process app. Undone at teardown."""
    from meiva.gateway_client import use_client

    with use_client(gateway_unit):
        yield gateway_unit
