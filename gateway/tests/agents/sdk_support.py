"""Shared real-route SDK apparatus and SQL setup for Gateway agent tests."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.packages.plugin_providers import build_model_catalog
from gateway.tests.auth.gateway_config import gateway_test_app, gateway_test_process
from gateway.tests.support.runner_ops.ops_stub import RunnerOps
from meiva.gateway_client import use_client
from meiva.tests.fixtures.pin_agent import pin_agent


def spawn_agent(
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
) -> int:
    """Setup helper — a row for the SDK's self identity (Task #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only need
    the row to exist)."""
    from base.agents.birth.spawn import create_agent_row

    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng()),
        machine=local_machine.name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    return agent_id


# meiva:note
# isolated test credential.
SDK_CLUSTER_SECRET = "sdk-test-secret"  # noqa: S105


@pytest.fixture(autouse=True)
def sdk_via_gateway(
    sdk_model_owner: None,
    gateway_local_runner: RunnerOps,
    *,
    machine_identity: MachineIdentityReader,
) -> Generator[TestClient]:
    """SDK <-> gateway path in-process: the SDK's gateway client is the test client of a real app.

    Calls go through ASGI directly into the gateway's endpoints, against the real database
    and Redis. The gateway's own machine is registered as a runner whose ops route runs in
    this process (`gateway_local_runner`), so a spawn or lifecycle request the gateway forwards
    to it is executed by the real op code. The SDK's installation is the one process-global
    slot on the `meiva` module, bound by `sdk_model_owner`.
    """
    _ = gateway_local_runner
    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process, cluster_secret=SDK_CLUSTER_SECRET)
        with (
            TestClient(
                app,
                base_url="http://test-gateway",
                headers={"Authorization": f"Bearer {SDK_CLUSTER_SECRET}"},
            ) as client,
            use_client(client),
        ):
            yield client


@pytest.fixture
def self_agent(
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> int:
    """The SDK caller's own agent: a committed row on the local machine, pinned as its identity."""
    agent_id = spawn_agent(
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    pin_agent(agent_id)
    return agent_id


# meiva:note
# The SDK's gateway client against a scripted gateway; yields the requests that reached it.
#
# For what the real gateway cannot say (an answer its own models refuse to produce) and for
# asserting that an invalid call never reached the wire. Without `answer`, a request is
# answered 500 and recorded, so the test fails on the assertion that none arrived.
@contextmanager
def gateway_wire(answer: httpx.Response | None = None) -> Generator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer if answer is not None else httpx.Response(500)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        use_client(http),
    ):
        yield requests


# meiva:note
# The SDK's configuration authority over a boot whose `fields` are set to the given values.
#
# A test overrides the `config_authority` fixture with this when the SDK resolves a default
# from configuration (`neighbors_default_depth`), instead of rewriting the process settings.
def authority_with(unit_home: Path, **fields: object) -> ConfigAuthority:
    boot = snapshot_process_config()
    for name, value in fields.items():
        boot.set_field(name, value)
    view = boot.view
    return ConfigAuthority(
        runtime=view,
        all_domains=view if view.profile is None else Settings(profile=None),
        env_path=unit_home / ".env",
    )


def agent_machine(db: psycopg.Connection, agent_id: int) -> str:
    row = db.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)).fetchone()
    assert row is not None
    return row[0]


# meiva:note
# (content, kind, source) of the agent's inbound messages, oldest first.
def inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]
