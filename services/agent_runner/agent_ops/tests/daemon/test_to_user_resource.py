import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import httpx
import pytest

from base.agents.to_user import read_document, to_user_limits
from base.config import settings
from base.daemon.http_transport import KeepAlive, start_daemon_http
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


async def test_ongoing_op_reads_only_the_workspace_root(
    ops: Ops, agent_id: int, unit_home: Path
) -> None:
    workspace = workspace_path(unit_home, agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "project").mkdir()
    (workspace / "project" / "ongoing.md").write_text("Nested document")
    (workspace / "to_user.md").write_text("Shared context")
    request = {"kind": "ongoing_read", "payload": {"agent_id": agent_id}}

    _, missing = await ops.route(request)
    assert missing["status"] == "completed"
    assert missing["result"] == {"content": None, "version": None, "modified_at": None}

    ongoing = workspace / "ongoing.md"
    ongoing.write_text(" \n")
    _, blank = await ops.route(request)
    assert blank["status"] == "completed"
    assert blank["result"]["content"] is None
    assert blank["result"]["modified_at"]
    assert blank["result"]["version"]

    content = "## Waiting\n- Approval from the maintainer\n"
    ongoing.write_text(content)
    _, present = await ops.route(request)
    assert present["status"] == "completed"
    assert present["result"]["content"] == content
    assert present["result"]["version"] != blank["result"]["version"]


# meiva:note
# The runner's real `/to-user/workspace-file` listener for one agent on `source-host`, behind the
# daemon transport and its bearer check; yields an authorized client and the agent's id. The
# admission sets are empty again once the reads finish.
@asynccontextmanager
async def _workspace_file_client(
    database: Database, unit_home: Path, op_executor: ThreadPoolExecutor
) -> AsyncGenerator[tuple[httpx.AsyncClient, int]]:
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
    (workspace / "src").mkdir()
    (workspace / "src" / "hello_test.py").write_text("", encoding="utf-8")
    (unit_home / "account").mkdir()

    requests: RequestTokens = set()
    workers: WorkerFutures = set()
    with database.pool() as pool:
        handler = partial(
            workspace_file_route,
            pool=pool,
            machine="source-host",
            home=unit_home,
            user_home=unit_home / "account",
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
            keep_alive=KeepAlive(),
        )
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                client.headers["Authorization"] = "Bearer test-token"
                yield client, agent_id
    assert not requests
    assert not workers


@pytest.mark.asyncio
async def test_workspace_file_route_content_and_raw(
    database: Database, unit_home: Path, op_executor: ThreadPoolExecutor
) -> None:
    async with _workspace_file_client(database, unit_home, op_executor) as (client, agent_id):
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


# meiva:note
# The forwarded reads the viewer adds: a byte range answers `206` with its `Content-Range` header
# through the daemon transport, and a listing answers JSON, reaching the account home when the
# gateway sends `all`.
@pytest.mark.asyncio
async def test_workspace_file_route_ranges_and_listing(
    database: Database, unit_home: Path, op_executor: ThreadPoolExecutor
) -> None:
    async with _workspace_file_client(database, unit_home, op_executor) as (client, agent_id):
        forwarded = {"agent_id": agent_id, "path": ""}
        range_resp = await client.post(
            "/to-user/workspace-file",
            json=forwarded | {"path": "icon.png", "mode": "raw", "byte_range": "bytes=1-3"},
        )
        assert (range_resp.status_code, range_resp.content) == (206, b"PNG")
        assert range_resp.headers["content-range"] == "bytes 1-3/11"

        list_resp = await client.post("/to-user/workspace-file", json=forwarded | {"mode": "list"})
        names = [entry["name"] for entry in list_resp.json()["entries"]]
        assert names == ["src", "hello.py", "icon.png"]

        home_resp = await client.post(
            "/to-user/workspace-file",
            json=forwarded | {"mode": "list", "path": "~", "file_access": "all"},
        )
        assert home_resp.json()["root"]["kind"] == "home"


@pytest.mark.asyncio
async def test_open_ended_ranges_stream_in_slices_over_one_connection(
    database: Database, unit_home: Path, op_executor: ThreadPoolExecutor
) -> None:
    slice_bytes = LIMITS.open_range_bytes
    media = bytes(i % 253 for i in range(slice_bytes * 2 + 12_345))
    async with _workspace_file_client(database, unit_home, op_executor) as (client, agent_id):
        (unit_home / "workspaces" / str(agent_id) / "clip.mp3").write_bytes(media)
        received = b""
        local_ports: set[int] = set()
        while len(received) < len(media):
            response = await client.post(
                "/to-user/workspace-file",
                json={
                    "agent_id": agent_id,
                    "path": "clip.mp3",
                    "mode": "raw",
                    "byte_range": f"bytes={len(received)}-",
                },
            )
            assert response.status_code == 206
            assert 0 < len(response.content) <= slice_bytes
            assert response.headers["content-length"] == str(len(response.content))
            first = len(received)
            last = first + len(response.content) - 1
            assert response.headers["content-range"] == f"bytes {first}-{last}/{len(media)}"
            received += response.content
            local_ports.add(response.extensions["network_stream"].get_extra_info("client_addr")[1])
        assert received == media
        assert len(local_ports) == 1
