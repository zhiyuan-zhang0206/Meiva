"""Bootstrap serves the authoritative hosted turn limit without rewriting zero."""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from base import config
from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.host.db_authority.api import AcceptanceCache
from base.host.env import bootstrap
from base.host.env.boot_policy import BootstrapFetch
from base.host.net.auth import bearer_header
from base.host.net.tests.local_http import serve_asgi
from base.lm.catalog import ModelCatalog
from gateway.cluster.bootstrap import router
from gateway.http.config import GatewayConfig

_SECRET = "bootstrap-admission-test"  # noqa: S105 — isolated test credential
_TURN_LIMIT = "MEIVA_HOST_MAX_CONCURRENT_TURNS"


# meiva:note
# The unit's `.env` as the gateway reads it: assigning an alias rewrites the real file.
class _UnitEnv:
    def __init__(self, path: Path, aliases: dict[str, str]) -> None:
        self._path = path
        self._aliases = aliases
        self._write()

    def __setitem__(self, alias: str, value: str) -> None:
        self._aliases[alias] = value
        self._write()

    def _write(self) -> None:
        self._path.write_text("".join(f'{k}="{v}"\n' for k, v in self._aliases.items()))


@pytest.fixture
def gateway_snapshot(unit_home: Path) -> _UnitEnv:
    return _UnitEnv(unit_home / ".env", {"MEIVA_DB_URL": str(config.settings.data_plane.db_url)})


@pytest.fixture
def served_authority(unit_home: Path) -> ConfigAuthority:
    """An authority whose hosted-agent limits are the gateway's own, read from its unit file."""
    boot = snapshot_process_config()
    boot.set_field("host_max_concurrent_turns", 0)
    boot.set_field("host_db_pool_max_size", 64)
    boot.set_field("host_control_pool_max_size", 8)
    complete = boot.view if boot.view.profile is None else Settings(profile=None)
    return ConfigAuthority(boot.view, complete, unit_home / ".env")


@pytest.fixture
def gateway_home(tmp_path: Path) -> Path:
    """The root the gateway process serves: its generation ledger lives here."""
    home = (tmp_path / "gateway-home").resolve()
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def runner_token(
    gateway_home: Path,
    seed_write_generation: Callable[[Path], Any],
) -> str:
    """The gateway's active write generation's runner API token, which a runner process is
    delivered: a runner never holds the human secret."""
    return seed_write_generation(gateway_home).api.runner


@pytest.fixture
def gateway_client(
    served_authority: ConfigAuthority, model_catalog: ModelCatalog, gateway_home: Path
) -> Iterator[TestClient]:
    """Own the isolated router's admission cache without a full Gateway lifespan."""
    app = FastAPI()
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.config_authority = served_authority
    app.state.catalog = model_catalog
    app.state.gateway_process = SimpleNamespace(home=gateway_home)
    app.state.gateway_config = GatewayConfig(
        cluster_secret=_SECRET,
        session_ttl_seconds=3600,
        mcp_endpoint_enabled=False,
        allowed_origins=(),
        browser_origin="",
        session_cookie_secure=False,
        memory_root=gateway_home / "memory",
        upload_home=gateway_home,
        human_auth="required",
    )
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("role", [None, "runner"])
@pytest.mark.parametrize(
    "raw", [None, "0", "00", "+0", " 0 ", "0.0", "7", "+007", "7.0", "invalid"]
)
def test_bootstrap_serves_turn_limit_verbatim(
    gateway_snapshot: _UnitEnv, gateway_client: TestClient, role: str | None, raw: str | None
) -> None:
    if raw is not None:
        gateway_snapshot[_TURN_LIMIT] = raw
    params = {"role": role} if role else {}
    response = gateway_client.get("/api/bootstrap", params=params, headers=bearer_header(_SECRET))
    assert response.status_code == 200
    served = response.json()
    assert served[_TURN_LIMIT] == (raw if raw is not None else "0")
    assert served["MEIVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert served["MEIVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


@pytest.mark.parametrize("secret", [None, "wrong-secret"])
def test_bootstrap_admission_preserves_auth(
    gateway_snapshot: _UnitEnv, gateway_client: TestClient, secret: str | None
) -> None:
    headers = bearer_header(secret) if secret else {}
    gateway_client.headers.pop("Authorization", None)
    response = gateway_client.get("/api/bootstrap", headers=headers)
    assert response.status_code == 401


# meiva:note
# An ASGI wrapper that notes the query string of every HTTP request it forwards.
class _RecordingApp:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.queries: list[bytes] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.queries.append(scope["query_string"])
        await self.app(scope, receive, send)


def test_runner_fetch_replaces_stale_admission(
    gateway_snapshot: _UnitEnv,
    gateway_client: TestClient,
    tmp_path: Path,
    runner_token: str,
) -> None:
    recorded = _RecordingApp(gateway_client.app)
    with serve_asgi(recorded) as url:
        env = {
            "MEIVA_GATEWAY_URL": url,
            "MEIVA_API_TOKEN": runner_token,
            _TURN_LIMIT: "16",
            "MEIVA_HOST_DB_POOL_MAX_SIZE": "20",
            "MEIVA_HOST_CONTROL_POOL_MAX_SIZE": "4",
        }
        bootstrap.inject_config_from_gateway(
            env, home=tmp_path, now=time.time(), tuning=BootstrapFetch()
        )
    # The fetch requests no projection: bootstrap serves configuration only.
    assert recorded.queries == [b""]
    assert env[_TURN_LIMIT] == "0"
    assert env["MEIVA_HOST_DB_POOL_MAX_SIZE"] == "64"
    assert env["MEIVA_HOST_CONTROL_POOL_MAX_SIZE"] == "8"


# meiva:note
# A snapshot from before the admission change (an older snapshot version) is never
# reused, whatever its age: the runner fetches and rewrites it.
@pytest.mark.parametrize("age_seconds", [1.0, 10_000.0], ids=["fresh", "stale"])
def test_runner_refreshes_an_outdated_admission_snapshot(
    gateway_snapshot: _UnitEnv,
    gateway_client: TestClient,
    tmp_path: Path,
    age_seconds: float,
    runner_token: str,
) -> None:
    now = time.time()
    with serve_asgi(gateway_client.app) as url:
        snapshot = bootstrap.snapshot_path(tmp_path)
        snapshot.parent.mkdir()
        snapshot.write_text(
            json.dumps(
                {
                    "v": 1,
                    "base_url": url,
                    "written_at": now - age_seconds,
                    "values": {_TURN_LIMIT: "16"},
                }
            )
        )
        env = {"MEIVA_GATEWAY_URL": url, "MEIVA_API_TOKEN": runner_token}
        assert bootstrap.read_config_snapshot(url, home=tmp_path, now=now) is None
        bootstrap.inject_config_from_gateway(env, home=tmp_path, now=now, tuning=BootstrapFetch())
        assert env[_TURN_LIMIT] == "0"
        written = bootstrap.read_config_snapshot(url, home=tmp_path, now=now)
    assert written is not None and written[0][_TURN_LIMIT] == "0"
