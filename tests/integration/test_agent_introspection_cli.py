# meiva:note
# Pin the published agent detail read through the CLI JSON consumer.

import json

import psycopg
import pytest

from base.lm.catalog import ModelCatalog
from cli.commands.agents.observe import cmd_agents_get
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving
from gateway.app import create_app


def test_get_json_preserves_the_real_agent_detail_contract(
    db_conn: psycopg.Connection,
    model_catalog: ModelCatalog,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from psycopg.types.json import Jsonb

    from gateway.tests.auth.gateway_config import (
        gateway_config_of,
        gateway_test_client,
        gateway_test_process,
    )

    overlay = {"reasoning_effort": "high"}
    db_conn.execute("INSERT INTO agents (id) VALUES (7)")
    db_conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, config_overlay, birth_config) "
        "VALUES (7, 'user', 'idling', %s, %s)",
        (Jsonb(overlay), Jsonb({"llm_model": "deepseek-v4-pro", "reasoning_effort": "low"})),
    )
    db_conn.commit()
    with gateway_test_process() as process:
        app = create_app(
            gateway_config_of(process.config),
            process=process,
            process_ownership="borrowed",
            catalog=model_catalog,
        )
        with gateway_test_client(app) as client:
            response = client.get("/api/agents/7")
    assert response.status_code == 200
    body = response.json()
    assert body["llm_model"] == "deepseek-v4-pro"
    assert body["reasoning_effort"] == "high"
    assert body["config_overlay"] == overlay
    with serving({("GET", "/api/agents/7"): Reply(body=body)}) as gateway:
        assert cmd_agents_get(access_to(gateway.url), 7, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == body
