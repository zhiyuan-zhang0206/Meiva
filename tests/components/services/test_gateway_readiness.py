import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from psycopg_pool import PoolTimeout

from base import config
from base.cluster.authority.tests.runner_logins import grant_runner_login
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.net.tests.local_http import serve_asgi
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.config_registration import plugin_bootstrap_config
from base.paths.home import resolve_meiva_home
from tests.fixtures.gateway_config import gateway_test_client


def test_gateway_health_probes_the_database(gateway_data_plane: None) -> None:
    with gateway_test_client() as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["name"] == "gateway"
        assert health.json()["status"] == "ok"


def test_gateway_health_reports_a_database_failure(gateway_data_plane: None) -> None:
    with gateway_test_client() as client:
        client.app.state.control_db_pool.connection = MagicMock(
            side_effect=PoolTimeout("isolated readiness failure")
        )
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "PoolTimeout" in response.text


def test_a_runners_first_join_reaches_the_gateway(
    tmp_path: Path,
    unit_home: Path,
    served_gateway_home: Any,
    gateway_data_plane: None,
    db_url: str,
) -> None:
    from base.host.db_authority import unit
    from cli.commands.cluster import unit_join

    secret = uuid4().hex
    runner_login = served_gateway_home.roles.runner
    grant_runner_login(
        db_url, owner="meiva_citest", login=runner_login.name, password=runner_login.password
    )
    complete = settings if settings.profile is None else config.Settings(profile=None)
    bootstrap_authority = ConfigAuthority(settings, complete, tmp_path / ".env")
    # meiva:note
    # The gateway serves the endpoint its home's `.env` names; the install's login probe
    # dials it, so it names the suite's Postgres.
    home_env = bootstrap_authority.read_env_aliases() | {"MEIVA_DB_URL": db_url}
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in home_env.items()))
    runner = (tmp_path / "runner").resolve()
    runner.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        resolve_meiva_home(),
        unit=unit.UnitIdentity(machine="mini", home=str(runner)),
        endpoint=config.bootstrap_config_values(
            bootstrap_authority,
            provider_key_envs=(
                binding.key_env for binding in build_model_catalog(home=unit_home).bindings.values()
            ),
            plugin_cluster_config=plugin_bootstrap_config(home=unit_home),
        )["MEIVA_DB_URL"],
        cluster_secret=secret,
        ttl_s=600,
        now=time.time(),
    )
    bundle = tmp_path / "mini.bundle"
    bundle.write_bytes(issued.envelope)
    with gateway_test_client(cluster_secret=secret) as client, serve_asgi(client.app) as gateway:
        unit_join.join_gateway(
            {"MEIVA_GATEWAY_URL": gateway, "MEIVA_MACHINE_NAME": "mini"},
            runner,
            str(bundle),
            issued.transport_key,
            now=time.time(),
        )
    installed = unit.require_unit_capability(runner)
    assert installed.api is not None and not bundle.exists()
