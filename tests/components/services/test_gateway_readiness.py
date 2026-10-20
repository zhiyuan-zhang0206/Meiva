import os
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from psycopg_pool import PoolTimeout

from base import config
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.env import dotenv_file
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.config_registration import plugin_bootstrap_config
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


def _authenticated(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = uuid4().hex
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr("base.paths.meiva_home", dotenv_file.unit_home)
    return secret


def test_a_runners_first_join_reaches_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unit_home: Path,
    served_gateway_home: Any,
    gateway_data_plane: None,
) -> None:
    del unit_home
    from base.host.db_authority import unit
    from base.host.env import bootstrap
    from cli.commands.cluster import unit_join

    secret = _authenticated(monkeypatch)
    complete = settings if settings.profile is None else config.Settings(profile=None)
    bootstrap_authority = ConfigAuthority(settings, complete, tmp_path / ".env")
    runner = (tmp_path / "runner").resolve()
    runner.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        dotenv_file.unit_home(),
        unit=unit.UnitIdentity(machine="mini", home=str(runner)),
        endpoint=config.bootstrap_config_values(
            bootstrap_authority,
            provider_key_envs=(
                binding.key_env for binding in build_model_catalog().bindings.values()
            ),
            plugin_cluster_config=plugin_bootstrap_config(),
        )["MEIVA_DB_URL"],
        cluster_secret=secret,
        ttl_s=600,
    )
    bundle = tmp_path / "mini.bundle"
    bundle.write_bytes(issued.envelope)
    monkeypatch.setattr(
        unit, "install_bundle", partial(unit.install_bundle, probe=lambda _dsn: None)
    )
    gateway = "http://127.0.0.1:1"
    with gateway_test_client() as client, patch.dict(os.environ):

        def dial(url: str, **kwargs: Any) -> Any:
            return client.get(url.removeprefix(gateway), **kwargs)

        monkeypatch.setattr(bootstrap, "dial_get", dial)
        os.environ[unit.CAPABILITY_KEY_ENV] = issued.transport_key
        unit_join.join_gateway(
            {"MEIVA_GATEWAY_URL": gateway, "MEIVA_MACHINE_NAME": "mini"}, runner, str(bundle)
        )
    installed = unit.require_unit_capability(runner)
    assert installed.api is not None and not bundle.exists()
