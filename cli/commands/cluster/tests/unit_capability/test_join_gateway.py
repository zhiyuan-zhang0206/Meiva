# meiva:note
# A remote unit's join, end to end against real resources.
#
# The gateway is a scripted listener on a loopback port that the join dials through its real
# transport and real headers (`gateway_server`); the bundle is sealed from a real private
# ledger; the install's login probe connects as the generation's runner role of the suite's
# throwaway Postgres. What the join refuses and which step takes which input are asserted on
# values in `test_unit_join_checks.py` and `test_unit_join_steps.py`; these tests prove the
# shell does it in that order: nothing is dialled before the credential is settled, nothing is
# installed or consumed unless the gateway and the cluster accepted it, and the served
# configuration is not kept.

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from base.host.db_authority.unit import (
    CAPABILITY_KEY_ENV,
    UnitCapabilityError,
    load_unit_capability,
    require_unit_capability,
    unit_capability_path,
)
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    BOOTSTRAP,
    MACHINE,
    bootstrap_reply,
    missing_database_endpoint,
    refused_endpoint,
    runner_api_token,
    seal_bundle,
)
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    database_endpoint as database_endpoint,
)
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    gateway_home as gateway_home,
)
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    runner_home as runner_home,
)
from cli.commands.cluster.unit_join import join_gateway


def _identity(gateway_url: str, **more: str) -> dict[str, str]:
    return {"MEIVA_GATEWAY_URL": gateway_url, "MEIVA_MACHINE_NAME": MACHINE, **more}


def test_a_carried_bundle_authenticates_the_fetch_then_is_installed_and_consumed(
    tmp_path: Path,
    gateway_home: Path,
    runner_home: Path,
    database_endpoint: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=database_endpoint, unit_home=runner_home
    )
    with serving({BOOTSTRAP: bootstrap_reply(database_endpoint)}) as gateway:
        values = _identity(gateway.url, MEIVA_DB_URL="postgresql://stale@elsewhere/db")

        join_gateway(values, runner_home, str(sealed.path), sealed.transport_key)

    (seen,) = gateway.requests
    assert seen.target == "/api/bootstrap"
    assert seen.headers["authorization"] == f"Bearer {runner_api_token(gateway_home)}"
    installed = require_unit_capability(runner_home)
    assert installed.unit.describe() == f"{MACHINE}:{runner_home}"
    assert installed.generation.number == 0
    assert not sealed.path.exists()
    out = capsys.readouterr().out
    assert "database capability installed: write generation 0" in out
    assert f"bundle {sealed.path} consumed" in out
    assert values == _identity(gateway.url)


def test_a_join_without_a_bundle_presents_the_installed_capability_and_installs_nothing(
    tmp_path: Path,
    gateway_home: Path,
    runner_home: Path,
    database_endpoint: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=database_endpoint, unit_home=runner_home
    )
    with serving({BOOTSTRAP: bootstrap_reply(database_endpoint)}) as gateway:
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), sealed.transport_key)
        installed = unit_capability_path(runner_home).read_bytes()
        capsys.readouterr()

        join_gateway(_identity(gateway.url), runner_home, None, "")

    first, second = gateway.requests
    assert second.headers["authorization"] == first.headers["authorization"]
    assert unit_capability_path(runner_home).read_bytes() == installed
    assert capsys.readouterr().out == ""


def test_a_unit_holding_no_capability_and_carrying_no_bundle_never_dials_the_gateway(
    runner_home: Path,
) -> None:
    with (
        serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway,
        pytest.raises(ValueError, match="holds no database capability"),
    ):
        join_gateway(_identity(gateway.url), runner_home, None, "")

    assert gateway.requests == []


def test_a_bundle_without_its_transport_key_is_refused_before_anything_is_dialled(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=refused_endpoint(), unit_home=runner_home
    )
    with (
        serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway,
        pytest.raises(ValueError, match=f"requires its transport key in {CAPABILITY_KEY_ENV}"),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), "")

    assert gateway.requests == [] and sealed.path.exists()


def test_a_bundle_the_transport_key_does_not_open_is_refused_before_anything_is_dialled(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    endpoint = refused_endpoint()
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=endpoint, unit_home=runner_home
    )
    other = seal_bundle(
        gateway_home, tmp_path / "other.bundle", endpoint=endpoint, unit_home=runner_home
    )
    with (
        serving({BOOTSTRAP: bootstrap_reply(endpoint)}) as gateway,
        pytest.raises(UnitCapabilityError, match="does not authenticate"),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), other.transport_key)

    assert gateway.requests == [] and sealed.path.exists()


def test_a_unit_holding_the_human_secret_is_refused_before_anything_is_dialled(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=refused_endpoint(), unit_home=runner_home
    )
    with serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway:
        values = {**_identity(gateway.url), "MEIVA_CLUSTER_SECRET": "the-human-secret"}
        with pytest.raises(ValueError, match="records the human cluster secret"):
            join_gateway(values, runner_home, str(sealed.path), sealed.transport_key)

    assert gateway.requests == [] and sealed.path.exists()


def test_a_gateway_that_refuses_the_bundles_token_leaves_the_bundle_and_the_unit_untouched(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=refused_endpoint(), unit_home=runner_home
    )
    with (
        serving({BOOTSTRAP: Reply(status=401, body={"detail": "no"})}) as gateway,
        pytest.raises(httpx.HTTPStatusError),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), sealed.transport_key)

    assert len(gateway.requests) == 1
    assert sealed.path.exists() and load_unit_capability(runner_home) is None


def test_a_gateway_that_serves_no_database_endpoint_installs_nothing(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=refused_endpoint(), unit_home=runner_home
    )
    with (
        serving({BOOTSTRAP: Reply(body={})}) as gateway,
        pytest.raises(ValueError, match="serves no MEIVA_DB_URL"),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), sealed.transport_key)

    assert sealed.path.exists() and load_unit_capability(runner_home) is None


def test_a_bundle_for_another_machine_is_refused_after_the_fetch_and_kept(
    tmp_path: Path, gateway_home: Path, runner_home: Path
) -> None:
    endpoint = refused_endpoint()
    sealed = seal_bundle(
        gateway_home,
        tmp_path / "other.bundle",
        endpoint=endpoint,
        unit_home=runner_home,
        machine="elsewhere",
    )
    with (
        serving({BOOTSTRAP: bootstrap_reply(endpoint)}) as gateway,
        pytest.raises(UnitCapabilityError, match="was issued for elsewhere:"),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), sealed.transport_key)

    assert len(gateway.requests) == 1
    assert sealed.path.exists() and load_unit_capability(runner_home) is None


def test_a_cluster_that_refuses_the_runner_login_installs_nothing_and_keeps_the_bundle(
    tmp_path: Path, gateway_home: Path, runner_home: Path, db_url: str
) -> None:
    endpoint = missing_database_endpoint(db_url)
    sealed = seal_bundle(
        gateway_home, tmp_path / "mini.bundle", endpoint=endpoint, unit_home=runner_home
    )
    with (
        serving({BOOTSTRAP: bootstrap_reply(endpoint)}) as gateway,
        pytest.raises(UnitCapabilityError, match="the cluster refuses generation 0's"),
    ):
        join_gateway(_identity(gateway.url), runner_home, str(sealed.path), sealed.transport_key)

    assert sealed.path.exists() and load_unit_capability(runner_home) is None
