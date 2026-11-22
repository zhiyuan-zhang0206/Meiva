"""A runner's bootstrap fetch against the live gateway endpoint."""

import asyncio
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil
import psycopg
import pytest
from mcp.types import TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from base import config
from base.agents.context.browser_identity import (
    BrowserAuthority,
    hosted_browser_identity,
    serve_browser_authority,
)
from base.clock.tests.fakes import system_clock
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.domains.storage.data_plane import self_machine_host
from base.host.boundary.json_values import is_object_dict
from base.host.env import bootstrap
from base.host.env.boot_policy import BootstrapFetch
from base.host.net.predicates import is_loopback_host
from base.host.net.tests.local_http import serve_asgi
from base.host.net.url_secret import url_with_host
from base.host.system.tests.fake_commands import fake_host
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import UnitState, encode, ok_response
from base.paths import root_run_dir
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.runner_ops.ops_stub import register_machine, runner_ops
from services.desktop.browser.routing.boot import browser_routes_from_boot
from services.desktop.browser.routing.session import RoutedBrowserSession

# meiva:note
# An isolated test credential, never a real secret.
_SECRET = "live-secret"  # noqa: S105


def test_fetch_bootstrap_config_against_live_endpoint(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    served_gateway_home: Any,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    # An authenticated gateway: the runner presents its delivered machine API
    # token (the active generation's runner token), never the human secret.
    # meiva:note
    # The runner dials the assembled gateway app through a real loopback listener.
    with (
        gateway_test_client(machine=machine_identity, cluster_secret=_SECRET) as client,
        serve_asgi(client.app) as url,
    ):
        values = bootstrap.fetch_bootstrap_config(
            url, bearer=served_gateway_home.api.runner, tuning=BootstrapFetch()
        )
    # Bootstrap serves the credential-free endpoint even though this gateway
    # keeps an active write generation: its runner login never travels here. A
    # multi-host gateway also rewrites its loopback host to the reachable
    # address for remote runners.
    runner = served_gateway_home.roles.runner
    expected = str(config.settings.data_plane.db_url)
    reachable = self_machine_host()
    if not is_loopback_host(reachable):
        expected = url_with_host(expected, reachable)
    actual_parts = urlsplit(values["MEIVA_DB_URL"])
    expected_parts = urlsplit(expected)
    # libpq dial hints such as hostaddr are implementation-specific query
    # parameters. The endpoint's connection identity must still match.
    assert (
        actual_parts.scheme,
        actual_parts.username,
        actual_parts.password,
        actual_parts.hostname,
        actual_parts.port,
        actual_parts.path.lstrip("/"),
    ) == (
        expected_parts.scheme,
        expected_parts.username,
        None,
        expected_parts.hostname,
        expected_parts.port,
        expected_parts.path.lstrip("/"),
    )
    assert runner.password not in "".join(values.values())
    # Nor does it serve the human secret: a remote unit never holds it.
    assert "MEIVA_CLUSTER_SECRET" not in values
    assert "test-cluster-secret" not in "".join(values.values())


def _register_browser_peers(db_conn: psycopg.Connection, endpoint: str) -> None:
    _ = db_conn.execute("TRUNCATE machines")
    db_conn.commit()
    register_machine(db_conn, "macbook-air", url=endpoint)
    register_machine(db_conn, "unreachable")
    register_machine(db_conn, "paused-peer", url=endpoint, paused=True)
    register_machine(db_conn, "stopped-peer", url=endpoint)
    register_machine(db_conn, "gateway-only", roles=("gateway",), url=endpoint)
    _ = db_conn.execute("UPDATE machines SET stopped_at = now() WHERE name = 'stopped-peer'")
    db_conn.commit()


def _boot_chrome_runner(short_tmp: Path, gateway: str, bearer: str) -> ConfigBoot:
    _ = (short_tmp / ".env").write_text(
        "MEIVA_MACHINE_NAME=macmini\nMEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n",
        encoding="utf-8",
    )
    env = {
        "MEIVA_HOME": str(short_tmp),
        "MEIVA_PROCESS_PROFILE": "runner",
        "MEIVA_GATEWAY_URL": gateway,
        "MEIVA_API_TOKEN": bearer,
    }
    assert "MEIVA_DB_URL" not in env
    assert "MEIVA_DB_GENERATION" not in env
    runner_config = ConfigBoot(environment=env)
    runner_config.boot()
    assert runner_config.view.env_boot.db_authority_refusal is not None
    return runner_config


async def test_runner_without_database_credentials_routes_list_pages(
    short_tmp: Path,
    db_conn: psycopg.Connection,
    served_gateway_home: Any,
    machine_identity: MachineIdentityReader,
) -> None:
    bearer: str = served_gateway_home.api.runner
    target = "meiva://macbook-air"
    owner = OwnedProcess.capture(psutil.Process())
    identity = {"pid": owner.pid, "create_time": owner.birth, "starttime": owner.starttime}
    root_path = root_run_dir(short_tmp) / "meiva-root.sock"
    root_path.parent.mkdir(parents=True)

    async def status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(
                encode(
                    ok_response(
                        {
                            "root": identity,
                            "units": [{"id": "agent-host", "state": UnitState.RUNNING, **identity}],
                        }
                    )
                )
            )
            await writer.drain()
        finally:
            writer.close()

    clock = system_clock()
    tokens = count()
    authority = BrowserAuthority(new_token=lambda: f"fixture-{next(tokens)}", now=clock.monotonic)
    caller = authority.new_caller(42)
    requests: list[dict[str, object]] = []

    async def peer(request: Request) -> JSONResponse:
        assert request.headers["authorization"] == f"Bearer {bearer}"
        body: object = await request.json()
        assert is_object_dict(body)
        requests.append(body)
        proof = body["proof"]
        assert isinstance(proof, str)
        claims = await asyncio.to_thread(
            hosted_browser_identity, short_tmp, "redeem", proof, target
        )
        assert claims["agent_id"] == 42
        return JSONResponse(
            {
                "ok": True,
                "result": {
                    "mcp": {"content": [{"type": "text", "text": "remote pages"}]},
                    "files": {},
                },
            }
        )

    async def local(_: dict[str, object]) -> object:
        pytest.fail("Cross-machine Chrome must not dial the local browser socket")

    async with (
        await asyncio.start_unix_server(status, path=root_path),
        await serve_browser_authority(short_tmp, authority),
    ):
        with serve_asgi(
            Starlette(routes=[Route("/browser/rpc", peer, methods=["POST"])])
        ) as endpoint:
            _register_browser_peers(db_conn, endpoint)
            with (
                runner_ops(db_conn, "unrelated-runner") as unrelated,
                gateway_test_client(
                    machine=machine_identity, cluster_secret=_SECRET, fields={"timezone": "UTC"}
                ) as client,
                serve_asgi(client.app) as gateway,
            ):
                runner_config = _boot_chrome_runner(short_tmp, gateway, bearer)
                routes = browser_routes_from_boot(
                    runner_config, home=short_tmp, timers=clock, host=fake_host()
                )
                assert routes.peers() == {
                    "macbook-air": endpoint,
                    "unreachable": None,
                    "unrelated-runner": unrelated.url,
                }
                session = RoutedBrowserSession(routes, local_request=local)
                session.client_agent_id = 42
                session.browser_caller = caller
                result = await session.call_tool("list_pages", {"host": target})
            assert unrelated.calls == []
        with pytest.raises(ValueError, match="proof rejected"):
            await asyncio.to_thread(
                hosted_browser_identity, short_tmp, "redeem", "fixture-1", target
            )

    assert result.content == [TextContent(type="text", text="remote pages")]
    assert requests == [
        {
            "method": "call_tool",
            "tool": "list_pages",
            "args": {},
            "origin": "macmini",
            "proof": "fixture-1",
            "inputs": {},
        }
    ]
