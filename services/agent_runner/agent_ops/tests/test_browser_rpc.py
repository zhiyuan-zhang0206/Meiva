import asyncio
import hashlib
import json
from itertools import count
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult

from base.agents.context.browser_identity import PURPOSE, BrowserAuthority
from base.daemon.http_transport import start_daemon_http
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.tests.short_tmp import short_tmp as short_tmp
from services.agent_runner.agent_ops.browser_rpc import BrowserRpc
from services.desktop.browser.routing.remote_files import MAX_RPC_BYTES, CallerFiles
from services.desktop.browser.routing.remote_rpc import BROWSER_RPC_ROUTE, call_browser_peer


async def test_browser_rpc_auth_upload_output_and_cleanup(short_tmp: Path) -> None:
    caller = short_tmp / "agent"
    caller.mkdir()
    (caller / "large.txt").write_bytes(b"x" * 100_000)
    seen_ids: list[object] = []

    async def serve_browser(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = object_of(json.loads, await reader.readline())
        assert is_object_dict(request)
        args = request["args"]
        assert is_object_dict(args)
        raw_path = args["filePath"]
        assert isinstance(raw_path, str)
        path = Path(raw_path)
        seen_ids.append(request["agent_id"])
        assert not path.is_absolute()
        if request["tool"] == "upload_file":
            assert request["inputs"] and path.name == "large.txt"
            files: dict[str, object] = {}
        else:
            files = {"filePath": [{"path": "output.txt", "data": "ZHVtbXkgc25hcHNob3Q="}]}
        reply = {
            "id": request["id"],
            "ok": True,
            "result": {
                "mcp": {"content": [{"type": "text", "text": "output.txt"}]},
                "files": files,
            },
        }
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    sock = short_tmp / "browser.sock"
    browser = await asyncio.start_unix_server(serve_browser, path=sock, limit=MAX_RPC_BYTES)
    ids = count()
    issuer = BrowserAuthority(new_token=lambda: str(next(ids)), now=lambda: 0.0)
    issuer.register(27, "fixture-secret")

    async def verify(origin: str, proof: str) -> object:
        assert origin == "ubuntu"
        return issuer.redeem(proof, "meiva://target", PURPOSE)

    rpc = BrowserRpc(sock, requests=set(), verify=verify)
    token = "fixture-key"  # noqa: S105 — fixture only
    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={BROWSER_RPC_ROUTE: rpc.route},
        body_limits={BROWSER_RPC_ROUTE: MAX_RPC_BYTES},
        auth_digests=frozenset({hashlib.sha256(token.encode()).hexdigest()}),
    )
    try:
        url = f"http://127.0.0.1:{next(iter(server.sockets or ())).getsockname()[1]}"
        async with httpx.AsyncClient() as client:
            unauthorized = await client.post(url + BROWSER_RPC_ROUTE[1], json={"method": "status"})
            assert unauthorized.status_code == 401

        for name, path in [("upload_file", "large.txt"), ("take_snapshot", "out.txt")]:
            await _roundtrip_artifact(
                url,
                token,
                caller,
                name,
                path,
                issuer.issue("fixture-secret", "meiva://target", PURPOSE)["proof"],
            )
        assert (caller / "out.txt").read_bytes() == b"dummy snapshot"
        with pytest.raises(RuntimeError, match="target-bound caller proof"):
            await call_browser_peer(
                url,
                {"Authorization": f"Bearer {token}"},
                {
                    "method": "call_tool",
                    "tool": "list_pages",
                    "agent_id": 27,
                    "args": {"host": "meiva://other"},
                },
                timeout_s=3,
            )
    finally:
        server.close()
        await server.wait_closed()
        browser.close()
        await browser.wait_closed()
    assert seen_ids == [27, 27]


async def _roundtrip_artifact(
    url: str, token: str, caller: Path, name: str, path: str, proof: object
) -> None:
    files = CallerFiles(caller, name, {"filePath": path})
    raw = await call_browser_peer(
        url,
        {"Authorization": f"Bearer {token}"},
        {
            "method": "call_tool",
            "tool": name,
            "origin": "ubuntu",
            "proof": proof,
            "args": files.args,
            "inputs": files.inputs,
        },
        timeout_s=3,
    )
    assert is_object_dict(raw)
    result = files.receive(CallToolResult.model_validate(raw["mcp"]), raw["files"])
    assert result.content[0].type == "text"


async def test_browser_admission_precedes_body_and_does_not_consume_ops_capacity() -> None:
    browser_started = asyncio.Event()
    finish_browser = asyncio.Event()
    ops = ("POST", "/ops")

    async def browser(_body: bytes) -> tuple[int, bytes, str]:
        browser_started.set()
        await finish_browser.wait()
        return 200, b"browser", "text/plain"

    async def lifecycle(_body: bytes) -> tuple[int, bytes, str]:
        return 200, b"lifecycle", "text/plain"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={BROWSER_RPC_ROUTE: browser, ops: lifecycle},
        body_slots={BROWSER_RPC_ROUTE: asyncio.Semaphore(1)},
        body_limits={BROWSER_RPC_ROUTE: 64},
    )
    try:
        sock = next(iter(server.sockets or ()))
        port = sock.getsockname()[1]
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
            asyncio.TaskGroup() as tasks,
        ):
            first = tasks.create_task(client.post(BROWSER_RPC_ROUTE[1], content=b"x"))
            try:
                await browser_started.wait()
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    writer.write(b"POST /browser/rpc HTTP/1.1\r\nContent-Length: 64\r\n\r\n")
                    await writer.drain()
                    status = await asyncio.wait_for(reader.readline(), timeout=1)
                    assert b"503" in status
                finally:
                    writer.close()
                    await writer.wait_closed()
                reply = await asyncio.wait_for(client.post(ops[1], content=b"{}"), timeout=1)
                assert reply.content == b"lifecycle"
            finally:
                finish_browser.set()
        assert first.result().content == b"browser"
    finally:
        server.close()
        await server.wait_closed()
