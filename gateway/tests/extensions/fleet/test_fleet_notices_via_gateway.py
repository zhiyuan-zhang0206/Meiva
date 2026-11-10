# meiva:note
# A question asked through the core `meiva.ui.ask` SDK lands in the gateway's stored question
# history and reaches the agent snapshot with its option order intact.

from collections.abc import Iterator

import psycopg
import pytest

from base.agents.observation.snapshot import select_one
from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from meiva import ui
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet.tests.registry_support import (
    installed_fleet_surface,
)


def seed_agent(db: psycopg.Connection) -> int:
    # meiva:note
    # Insert an agents + agents_meta row (the real spawn path always creates it first).
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id: int = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (agent_id,),
        )
    db.commit()
    return agent_id


@pytest.fixture
def sdk_via_inprocess_gateway(
    gateway_data_plane: None,
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentityReader,
):
    """Route the SDK's gateway client at the in-process app so ask hits the real endpoint
    against the test DB.

    The assembled Gateway reads both data stores, so the fixture declares
    `gateway_data_plane`."""
    from gateway.tests.auth.gateway_config import gateway_test_client
    from meiva.gateway_client import use_client

    with (
        gateway_test_client(base_url="http://test-gateway", machine=machine_identity) as tc,
        use_client(tc),
    ):
        yield


@pytest.fixture
def load_activity_plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install Fleet with a valid local sampling policy; uninstall after the test.

    Retained patch: the SDK installation's sampling owner refreshes from the gateway's
    bootstrap fetch, and `installed_fleet_surface` (meiva_builtins) does not take a
    `sampling=` owner to install instead.
    """

    with installed_fleet_surface():
        yield


def test_sdk_question_keeps_number_order_in_snapshot(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
) -> None:
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    ui.ask(
        "Choose the scope?",
        question={
            "type": "single_select",
            "options": [{"id": "z", "label": "First"}, {"id": "a", "label": "Second"}],
        },
    )
    db_conn.rollback()
    snapshot = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(home=meiva_home()),
        default_model=settings.lm.llm_model,
    )
    assert snapshot is not None
    question = snapshot.notices_awaiting_response[0].question
    assert question is not None
    assert question.model_dump(mode="json")["options"] == [
        {"id": "z", "label": "First", "description": None},
        {"id": "a", "label": "Second", "description": None},
    ]
