import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import httpx
import pytest

from base.agents.to_user import read_document, to_user_limits
from base.config import settings
from base.daemon.http_transport import start_daemon_http
from base.db import Database, create_agent
from base.paths import workspace_path
from ops.to_user import read_to_user
from services.agent_runner.agent_ops.in_flight import RequestTokens, WorkerFutures
from services.agent_runner.agent_ops.tests.ops_support import Ops
from services.agent_runner.agent_ops.to_user_resource import resource_route, workspace_file_route

# meiva:note
# The read caps the route and the door take as a value, at the configured numbers.
LIMITS = to_user_limits(settings.to_user)


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
    resource = next(iter(read_document(workspace, limits=LIMITS).resources.values()))
    # meiva:note
    # The file sits beside the workspace, not inside it, and beside a home that is not this
    # process's: its readable name is the absolute path the document already wrote.
    assert resource.file == str(outside)
    payload = {"agent_id": agent_id, "files": [resource.file]}
    requests: RequestTokens = set()
    workers: WorkerFutures = set()
    with database.pool() as pool:
        assert read_to_user(
            pool, agent_id, "source-host", files=[], home=unit_home, limits=LIMITS
        ).content
        with pytest.raises(ValueError, match="home machine"):
            read_to_user(pool, agent_id, "wrong-host", files=[], home=unit_home, limits=LIMITS)
        handler = partial(
            resource_route,
            pool=pool,
            machine="source-host",
            home=unit_home,
            limits=LIMITS,
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
                # meiva:note
                # A readable name makes the absolute path of the file well formed, so the
                # refusal is the document's whitelist, not the shape: `gone`, not `invalid`.
                assert (
                    await client.post(
                        "/to-user/resource",
                        content=json.dumps({"agent_id": agent_id, "files": [str(outside)]}),
                    )
                ).status_code == 404
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
    oversized = read_document(workspace, limits=LIMITS).resources["big.md"].file

    _, too_large = await ops.route(
        {"kind": "to_user_read", "payload": {"agent_id": agent_id, "files": [oversized]}}
    )
    assert too_large["status"] == "failed"
    assert too_large["result"]["reason"] == "too_large"

    _, gone = await ops.route(
        {"kind": "to_user_read", "payload": {"agent_id": agent_id, "files": ["missing.md"]}}
    )
    assert gone["status"] == "failed"
    assert gone["result"]["reason"] == "gone"


@pytest.mark.asyncio
async def test_workspace_file_route_content_and_raw(
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
    (workspace / "hello.py").write_text("print('remote')\n", encoding="utf-8")
    (workspace / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\nraw")

    requests: RequestTokens = set()
    workers: WorkerFutures = set()
    with database.pool() as pool:
        handler = partial(
            workspace_file_route,
            pool=pool,
            machine="source-host",
            home=unit_home,
            limits=LIMITS,
            executor=op_executor,
            dispatch_sem=asyncio.Semaphore(1),
            requests=requests,
            workers=workers,
        )
        server = await start_daemon_http(
            host="127.0.0.1",
            port=0,
            health_response=lambda: (200, b"{}"),
            extra_routes={("POST", "/to-user/workspace-file"): handler},
            auth_digests=frozenset({hashlib.sha256(b"test-token").hexdigest()}),
        )
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                client.headers["Authorization"] = "Bearer test-token"
                content_resp = await client.post(
                    "/to-user/workspace-file",
                    json={"agent_id": agent_id, "path": "hello.py#L1", "mode": "content"},
                )
                assert content_resp.status_code == 200
                body = content_resp.json()
                assert body["name"] == "hello.py"
                assert body["content"] == "print('remote')\n"
                assert body["binary"] is False

                raw_resp = await client.post(
                    "/to-user/workspace-file",
                    json={"agent_id": agent_id, "path": "icon.png", "mode": "raw"},
                )
                assert raw_resp.status_code == 200
                assert raw_resp.content == b"\x89PNG\r\n\x1a\nraw"

                traversal_resp = await client.post(
                    "/to-user/workspace-file",
                    json={"agent_id": agent_id, "path": "../../etc/passwd", "mode": "content"},
                )
                assert traversal_resp.status_code == 403

                missing_resp = await client.post(
                    "/to-user/workspace-file",
                    json={"agent_id": agent_id, "path": "missing.py", "mode": "content"},
                )
                assert missing_resp.status_code == 404
    assert not requests
    assert not workers
