from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.cluster.authority.tests.unit_capability_support import ENDPOINT
from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import (
    runner_capability as runner_capability,
)
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from base.db.connections import NoDatabaseAuthorityError, guard_db_url
from base.host.db_authority import delivery, unit
from base.host.db_authority.api import API_TOKEN_ENV
from base.host.env import bootstrap, dotenv_boot

_REPO = Path(__file__).resolve().parents[2]


def _boot_with_bootstrap(env: dict[str, str], home: Path) -> dotenv_boot.EnvBootResult:
    dotenv_boot.enforce_cluster_env_authority(env, home)
    result = bootstrap.deliver_unit_authority(env, home=home)
    bootstrap.apply_bootstrap_values(
        env,
        "http://gateway.invalid",
        {"MEIVA_DB_URL": ENDPOINT, "MEIVA_EVENTS_CHANNEL": "meiva:events"},
        home=home,
    )
    return result


def test_a_launched_service_keeps_its_unit_login_over_the_bootstrap_endpoint(
    runner_capability: unit.UnitCapability, runner_home: Path
) -> None:
    env = {"MEIVA_PROCESS_PROFILE": "runner"}
    env.update(unit.unit_delivery(runner_home))
    env.update(unit.unit_api_delivery(runner_home))
    result = _boot_with_bootstrap(env, runner_home)
    assert env["MEIVA_DB_URL"] == runner_capability.dsn
    assert env[delivery.GENERATION_ENV] == "0"
    assert runner_capability.api is not None and env[API_TOKEN_ENV] == runner_capability.api.token
    assert result.db_authority_refusal is None


def test_a_remote_unit_drops_an_inherited_human_secret(
    runner_capability: unit.UnitCapability, runner_home: Path
) -> None:
    del runner_capability
    env = {"MEIVA_CLUSTER_SECRET": "inherited-" + "x" * 32}
    _boot_with_bootstrap(env, runner_home)
    assert "MEIVA_CLUSTER_SECRET" not in env


def test_a_forged_login_is_replaced_by_the_endpoint_and_refused(
    runner_capability: unit.UnitCapability, runner_home: Path
) -> None:
    del runner_capability
    env = {
        "MEIVA_PROCESS_PROFILE": "runner",
        "MEIVA_DB_URL": "postgresql://meiva_g0_runner:forged@10.0.0.7:6433/meiva",
        delivery.GENERATION_ENV: "0",
    }
    result = _boot_with_bootstrap(env, runner_home)
    assert env["MEIVA_DB_URL"] == ENDPOINT
    refusal = result.db_authority_refusal
    assert refusal is not None and "only the root launcher delivers" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="credential-free"):
        guard_db_url(ENDPOINT, refusal=refusal)


def test_an_admitted_operator_process_consumes_the_installed_login(
    runner_capability: unit.UnitCapability, runner_home: Path
) -> None:
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    env: dict[str, str] = {}
    result = _boot_with_bootstrap(env, runner_home)
    assert env["MEIVA_DB_URL"] == runner_capability.dsn
    assert env[delivery.GENERATION_ENV] == "0"
    assert runner_capability.api is not None and env[API_TOKEN_ENV] == runner_capability.api.token
    assert result.db_authority_refusal is None


def test_a_runner_without_a_capability_is_refused_by_name(
    runner_capability: unit.UnitCapability, runner_home: Path
) -> None:
    del runner_capability
    intent = runner_home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(runner_home), "checkout": str(_REPO)}))
    intent.chmod(0o600)
    unit.unit_capability_path(runner_home).unlink()
    env: dict[str, str] = {}
    result = _boot_with_bootstrap(env, runner_home)
    assert env["MEIVA_DB_URL"] == ENDPOINT
    assert API_TOKEN_ENV not in env
    refusal = result.db_authority_refusal
    assert refusal is not None and "holds no database capability" in refusal
    assert "meiva cluster db-authority issue-unit" in refusal
    with pytest.raises(NoDatabaseAuthorityError, match="issue-unit"):
        guard_db_url(ENDPOINT, refusal=refusal)
