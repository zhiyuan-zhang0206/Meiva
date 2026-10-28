import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import httpx
import pytest

from base.agents.to_user import read_document
from base.config import settings
from base.daemon.http_transport import start_daemon_http
from base.db import Database, create_agent
from base.paths import workspace_path
from ops.to_user import read_to_user
from services.agent_runner.agent_ops.in_flight import RequestTokens, WorkerFutures
from services.agent_runner.agent_ops.tests.ops_support import Ops
from services.agent_runner.agent_ops.to_user_resource import resource_route


@pytest.mark.asyncio
async def test_authenticated_home_machine_file_roundtrip(
    database: Database, unit_home: Path, op_executor: ThreadPoolExecutor
) -> None:
    with database.connect() as conn:
        agent_id = create_agent(conn)
        conn.execute(
            "INSERT INTO agents_meta (id, machine, status) VALUES (%s, 'source-host', 'idling')",
            (agent_id,),
        )
        conn.commit()
    workspace = unit_home / "workspaces" / str(agent_id)
    workspace.mkdir(parents=True)
    outside = unit_home / "report.bin"
    outside.write_bytes(b"\x00\xffreport")
    (workspace / "to_user.md").write_text(f"[report]({outside})")
    resource = next(iter(read_document(workspace).resources.values()))
    payload = {"agent_id": agent_id, "references": [resource.id]}
    requests: RequestTokens = set()
    workers: WorkerFutures = set()
    with database.pool() as pool:
        assert read_to_user(pool, agent_id, "source-host", references=[], home=unit_home).content
        with pytest.raises(ValueError, match="home machine"):
            read_to_user(pool, agent_id, "wrong-host", references=[], home=unit_home)
        handler = partial(
            resource_route,
            pool=pool,
            machine="source-host",
            home=unit_home,
            executor=op_executor,
            dispatch_sem=asyncio.Semaphore(1),
            requests=requests,
            workers=workers,
        )
        server = await start_daemon_http(
            host="127.0.0.1",
            port=0,
            health_response=lambda: (200, b"{}"),
            extra_routes={("POST", "/to-user/resource"): handler},
            auth_digests=frozenset({hashlib.sha256(b"test-token").hexdigest()}),
        )
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                assert (await client.post("/to-user/resource", json=payload)).status_code == 401
                client.headers["Authorization"] = "Bearer test-token"
                response = await client.post("/to-user/resource", json=payload)
                assert response.status_code == 200
                assert response.content == b"\x00\xffreport"
                assert response.headers["content-type"] == "application/octet-stream"
                (workspace / "to_user.md").write_text("Link removed")
                assert (await client.post("/to-user/resource", json=payload)).status_code == 404
                assert (
                    await client.post(
                        "/to-user/resource",
                        content=json.dumps({"agent_id": agent_id, "references": [str(outside)]}),
                    )
                ).status_code == 422
    assert not requests
    assert not workers


# meiva:note
# The document read — what a panel click reaches — answers its class too: a refused
# `to_user_read` op reports the reason the gateway names, not a bare failure.
async def test_a_refused_document_read_reports_its_class(
    ops: Ops, agent_id: int, unit_home: Path
) -> None:
    workspace = workspace_path(unit_home, agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "big.md").open("wb") as stream:
        stream.truncate(settings.to_user.max_document_bytes + 1)
    (workspace / "to_user.md").write_text("[big](big.md)")
    oversized = read_document(workspace).resources["big.md"].id

    _, too_large = await ops.route(
        {"kind": "to_user_read", "payload": {"agent_id": agent_id, "references": [oversized]}}
    )
    assert too_large["status"] == "failed"
    assert too_large["result"]["reason"] == "too_large"

    _, gone = await ops.route(
        {"kind": "to_user_read", "payload": {"agent_id": agent_id, "references": ["0" * 64]}}
    )
    assert gone["status"] == "failed"
    assert gone["result"]["reason"] == "gone"
