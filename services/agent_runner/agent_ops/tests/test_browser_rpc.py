import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult

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
    paths: list[Path] = []

    async def serve_browser(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = object_of(json.loads, await reader.readline())
        assert is_object_dict(request)
        assert request["agent_id"] == 27
        assert request["method"] == "call_remote_tool"
        args = request["args"]
        assert is_object_dict(args)
        raw_path = args["filePath"]
        assert isinstance(raw_path, str)
        path = Path(raw_path)
        paths.append(path)
        assert path.is_relative_to(short_tmp) and not path.is_relative_to(caller)
        if request["tool"] == "upload_file":
            assert path.name == "large.txt"
            assert path.read_bytes() == b"x" * 100_000
        else:
            path.write_bytes(b"dummy snapshot")
        reply = {
            "id": request["id"],
            "ok": True,
            "result": {"content": [{"type": "text", "text": str(path)}]},
        }
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    sock = short_tmp / "browser.sock"
    browser = await asyncio.start_unix_server(serve_browser, path=sock)
    rpc = BrowserRpc(sock, dispatch_sem=asyncio.Semaphore(1), requests=set())
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
            await _roundtrip_artifact(url, token, caller, name, path)
        assert (caller / "out.txt").read_bytes() == b"dummy snapshot"
        assert paths[0].exists()
        assert not paths[1].exists()
        with pytest.raises(RuntimeError, match="nested host"):
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
        rpc.close()
        browser.close()
        await browser.wait_closed()
    assert paths and not any(path.exists() for path in paths)


async def _roundtrip_artifact(url: str, token: str, caller: Path, name: str, path: str) -> None:
    files = CallerFiles(caller, name, {"filePath": path})
    raw = await call_browser_peer(
        url,
        {"Authorization": f"Bearer {token}"},
        {
            "method": "call_tool",
            "tool": name,
            "agent_id": 27,
            "args": files.args,
            "inputs": files.inputs,
        },
        timeout_s=3,
    )
    assert is_object_dict(raw)
    result = files.receive(CallToolResult.model_validate(raw["mcp"]), raw["files"])
    assert result.content[0].type == "text"
