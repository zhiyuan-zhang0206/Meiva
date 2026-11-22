from dataclasses import replace
from pathlib import Path

import pytest
from mcp.types import TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.host.net.tests.local_http import serve_asgi
from base.host.system.tests.fake_commands import fake_host
from services.desktop.browser.routing.boot import browser_routes_from_boot
from services.desktop.browser.routing.session import RoutedBrowserSession


async def test_runner_without_database_credentials_routes_list_pages(tmp_path: Path) -> None:
    endpoint = "https://macbook-air.example.test"
    rows = [
        {
            "name": name,
            "gateway_url": url,
            "serve_gateway": False,
            "serve_agent_runner": True,
            "up_since_at": "2026-11-23T00:00:00Z",
            "online": False,
            "paused": None,
        }
        for name, url in [("macbook-air", endpoint), ("unreachable", "")]
    ]
    requests: list[str] = []

    async def gateway_reply(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != "Bearer fixture-runner-token":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        requests.append(request.url.path)
        if request.url.path == "/api/bootstrap":
            return JSONResponse(
                {
                    "MEIVA_DB_URL": "postgresql://127.0.0.1:1/meiva",
                    "MEIVA_REDIS_URL": "redis://127.0.0.1:1/0",
                    "MEIVA_TIMEZONE": "UTC",
                }
            )
        return JSONResponse(rows)

    app = Starlette(
        routes=[
            Route("/api/bootstrap", gateway_reply),
            Route("/api/cluster/roster", gateway_reply),
        ]
    )
    with serve_asgi(app) as gateway:
        (tmp_path / ".env").write_text(
            "MEIVA_MACHINE_NAME=macmini\n"
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            "MEIVA_BROWSER_ENABLED=false\n",
            encoding="utf-8",
        )
        env = {
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_PROCESS_PROFILE": "runner",
            "MEIVA_GATEWAY_URL": gateway,
            "MEIVA_API_TOKEN": "fixture-runner-token",
        }
        assert "MEIVA_DB_URL" not in env
        assert "MEIVA_DB_GENERATION" not in env
        config = ConfigBoot(environment=env)
        config.boot()
        assert config.view.env_boot.db_authority_refusal is not None
        routes = browser_routes_from_boot(
            config, home=tmp_path, timers=system_clock(), host=fake_host()
        )
        assert routes.peers() == {"macbook-air": endpoint, "unreachable": None}

        claims: list[tuple[str, str, str]] = []

        def verify(method: str, caller: str, target: str) -> dict[str, object]:
            claims.append((method, caller, target))
            return (
                {"agent_id": 42, "proof": "fixture-proof"}
                if method == "issue"
                else {"agent_id": 42}
            )

        async def local(_: dict[str, object]) -> object:
            pytest.fail("Cross-machine Chrome must not dial the local browser socket")

        async def peer(url: str, headers: object, request: object) -> object:
            assert url == endpoint
            assert headers == {"Authorization": "Bearer fixture-runner-token"}
            assert request == {
                "method": "call_tool",
                "tool": "list_pages",
                "args": {},
                "origin": "macmini",
                "proof": "fixture-proof",
                "inputs": {},
            }
            return {"mcp": {"content": [{"type": "text", "text": "remote pages"}]}, "files": {}}

        session = RoutedBrowserSession(
            replace(routes, verify_caller=verify), local_request=local, peer_request=peer
        )
        session.client_agent_id = 42
        session.browser_caller = "fixture-caller"
        result = await session.call_tool("list_pages", {"host": "meiva://macbook-air"})

    assert result.content == [TextContent(type="text", text="remote pages")]
    assert claims == [
        ("identify", "fixture-caller", ""),
        ("issue", "fixture-caller", "meiva://macbook-air"),
    ]
    assert requests == ["/api/bootstrap", "/api/cluster/roster", "/api/cluster/roster"]
