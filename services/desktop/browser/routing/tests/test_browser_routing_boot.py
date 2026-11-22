import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from base.api_contracts.contracts import MACHINE_ENDPOINTS_PATH
from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.host.net.tests.local_http import serve_asgi
from base.host.system.tests.fake_commands import fake_host
from services.desktop.browser.routing.boot import browser_routes_from_boot
from services.desktop.browser.routing.session import BrowserRoutes

_BEARER = "Bearer fixture-runner-token"


@contextmanager
def _runner(
    home: Path, *, directory: object, status: int = 200, raw: bytes | None = None
) -> Generator[BrowserRoutes]:
    async def bootstrap(request: Request) -> JSONResponse:
        assert request.headers["authorization"] == _BEARER
        return JSONResponse(
            {
                "MEIVA_DB_URL": "postgresql://127.0.0.1:1/meiva",
                "MEIVA_REDIS_URL": "redis://127.0.0.1:1/0",
                "MEIVA_TIMEZONE": "UTC",
            }
        )

    async def endpoints(request: Request) -> Response:
        assert request.headers["authorization"] == _BEARER
        if status == 504:
            await asyncio.sleep(5.2)
        if raw is not None:
            return Response(raw)
        return JSONResponse(directory, status_code=status)

    app = Starlette(
        routes=[Route("/api/bootstrap", bootstrap), Route(MACHINE_ENDPOINTS_PATH, endpoints)]
    )
    with serve_asgi(app) as gateway:
        _ = (home / ".env").write_text(
            "MEIVA_MACHINE_NAME=macmini\n"
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
            "MEIVA_BROWSER_ENABLED=false\n",
            encoding="utf-8",
        )
        env = {
            "MEIVA_HOME": str(home),
            "MEIVA_PROCESS_PROFILE": "runner",
            "MEIVA_GATEWAY_URL": gateway,
            "MEIVA_API_TOKEN": "fixture-runner-token",
        }
        assert "MEIVA_DB_URL" not in env
        assert "MEIVA_DB_GENERATION" not in env
        config = ConfigBoot(environment=env)
        config.boot()
        assert config.view.env_boot.db_authority_refusal is not None
        yield browser_routes_from_boot(config, home=home, timers=system_clock(), host=fake_host())


@pytest.mark.parametrize(
    ("status", "raw", "error", "match"),
    [
        (401, None, RuntimeError, "directory returned HTTP 401"),
        (503, None, RuntimeError, "directory returned HTTP 503"),
        (504, None, RuntimeError, "directory request failed \\(ReadTimeout\\)"),
        (200, b"", ValueError, "directory returned an invalid response"),
        (200, b"{bad json", ValueError, "directory returned an invalid response"),
    ],
)
def test_peer_directory_reports_remote_failures(
    tmp_path: Path, status: int, raw: bytes | None, error: type[Exception], match: str
) -> None:
    with (
        _runner(tmp_path, directory={}, status=status, raw=raw) as routes,
        pytest.raises(error, match=match),
    ):
        _ = routes.peers()
