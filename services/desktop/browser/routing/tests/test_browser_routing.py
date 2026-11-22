from pathlib import Path

import pytest

from base.clock.tests.fakes import system_clock
from services.desktop.browser.routing.session import BrowserRoutes, RoutedBrowserSession


async def test_headless_listing_and_remote_call_preserve_caller_and_strip_host(
    tmp_path: Path,
) -> None:
    received: list[tuple[str, object, object]] = []
    identity_calls: list[str] = []

    def identity(method: str, _caller: str, _target: str) -> dict[str, object]:
        identity_calls.append(method)
        return {"agent_id": 427, **({"proof": "proof-127"} if method == "issue" else {})}

    async def local(_: dict[str, object]) -> object:
        pytest.fail("headless Chrome must not dial a local socket")

    async def peer(url: str, headers: object, request: object) -> object:
        received.append((url, headers, request))
        if request == {"method": "list_tools"}:
            return [{"name": "take_snapshot", "inputSchema": {"type": "object"}}]
        return {"mcp": {"content": [{"type": "text", "text": "remote"}]}, "files": {}}

    routes = BrowserRoutes(
        home=tmp_path,
        local_machine="ubuntu",
        local_available=lambda: False,
        peers=lambda: {"macmini": "https://runner.test"},
        headers=lambda: {"Authorization": "Bearer fixture"},
        timers=system_clock(),
        connect_timeout_s=2,
        verify_caller=identity,
    )
    session = RoutedBrowserSession(routes, local_request=local, peer_request=peer)
    session.client_agent_id = 427
    session.browser_caller = "verified-fixture"
    tools = await session.list_tools()
    assert "host" in tools.tools[0].input_schema["properties"]
    with pytest.raises(RuntimeError, match="Local Chrome is unavailable"):
        await session.call_tool("take_snapshot", {})
    with pytest.raises(ValueError, match="workspace"):
        await session.call_tool(
            "upload_file", {"host": "meiva://macmini", "filePath": "missing.txt"}
        )
    assert identity_calls == ["identify"]
    result = await session.call_tool("take_snapshot", {"host": "meiva://macmini"})
    assert identity_calls == ["identify", "identify", "issue"]
    assert result.content[0].type == "text"
    assert result.content[0].text == "remote"
    assert received[-1] == (
        "https://runner.test",
        {"Authorization": "Bearer fixture"},
        {
            "method": "call_tool",
            "tool": "take_snapshot",
            "origin": "ubuntu",
            "proof": "proof-127",
            "args": {},
            "inputs": {},
        },
    )


@pytest.mark.parametrize(
    "host", ["10.0.0.2", "ssh://missing", "meiva://../host", "http://other", "local\n"]
)
async def test_invalid_host_fails_before_network_access(tmp_path: Path, host: str) -> None:
    async def impossible(_: dict[str, object]) -> object:
        pytest.fail("invalid host must not reach a local browser")

    def impossible_peers() -> dict[str, str]:
        pytest.fail("invalid selectors must not touch the data plane")

    routes = BrowserRoutes(
        home=tmp_path,
        local_machine="ubuntu",
        local_available=lambda: False,
        peers=impossible_peers,
        headers=dict,
        timers=system_clock(),
        connect_timeout_s=2,
    )
    session = RoutedBrowserSession(routes, local_request=impossible)
    session.client_agent_id = 12
    with pytest.raises(ValueError):
        await session.call_tool("take_snapshot", {"host": host})
