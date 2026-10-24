# meiva:note
# `meiva cluster db-authority install-unit`, end to end against real resources.
#
# An initialized runner home on disk, a bundle sealed from a real private ledger, a scripted
# gateway on a loopback port that answers the bootstrap fetch, and the suite's throwaway
# Postgres as the database the bundle's runner login connects to. The refusals and their
# wording are asserted on values in `test_db_authority_replies.py`; these tests prove the verb
# exits and leaves the unit and the bundle as they say.

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.db_authority.unit import load_unit_capability, require_unit_capability
from cli.commands.cluster.control import cmd_db_authority_install_unit
from cli.commands.cluster.tests.gateway_server import serving
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    BOOTSTRAP,
    bootstrap_reply,
    initialized_runner,
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


def test_it_installs_the_bundle_deletes_it_and_presents_the_bundles_token(
    tmp_path: Path, gateway_home: Path, database_endpoint: str
) -> None:
    with serving({BOOTSTRAP: bootstrap_reply(database_endpoint)}) as gateway:
        home = initialized_runner(tmp_path, gateway_url=gateway.url)
        sealed = seal_bundle(
            gateway_home, tmp_path / "unit.bundle", endpoint=database_endpoint, unit_home=home
        )

        rc = cmd_db_authority_install_unit(
            bundle=str(sealed.path), transport_key=sealed.transport_key, home=home
        )

    assert rc == 0
    (seen,) = gateway.requests
    assert seen.headers["authorization"] == f"Bearer {runner_api_token(gateway_home)}"
    assert require_unit_capability(home).api.token == runner_api_token(gateway_home)
    assert not sealed.path.exists()


def test_a_fresh_bundle_of_the_same_generation_renews_the_installed_one(
    tmp_path: Path, gateway_home: Path, database_endpoint: str
) -> None:
    with serving({BOOTSTRAP: bootstrap_reply(database_endpoint)}) as gateway:
        home = initialized_runner(tmp_path, gateway_url=gateway.url)
        first = seal_bundle(
            gateway_home, tmp_path / "first.bundle", endpoint=database_endpoint, unit_home=home
        )
        assert (
            cmd_db_authority_install_unit(
                bundle=str(first.path), transport_key=first.transport_key, home=home
            )
            == 0
        )
        installed = require_unit_capability(home)

        renewal = seal_bundle(
            gateway_home, tmp_path / "renewal.bundle", endpoint=database_endpoint, unit_home=home
        )
        rc = cmd_db_authority_install_unit(
            bundle=str(renewal.path), transport_key=renewal.transport_key, home=home
        )

    assert rc == 0
    renewed = require_unit_capability(home)
    assert renewed.generation == installed.generation and renewed.bundle != installed.bundle
    assert not renewal.path.exists() and len(gateway.requests) == 2


def test_it_refuses_a_gateway_home_and_keeps_the_bundle(
    tmp_path: Path, gateway_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway:
        home = initialized_runner(
            tmp_path, gateway_url=gateway.url, roles=frozenset({"gateway", "agent-runner"})
        )
        sealed = seal_bundle(
            gateway_home, tmp_path / "unit.bundle", endpoint=refused_endpoint(), unit_home=home
        )

        rc = cmd_db_authority_install_unit(
            bundle=str(sealed.path), transport_key=sealed.transport_key, home=home
        )

    assert rc == 1
    assert "keeps its own write-generation ledger" in capsys.readouterr().err
    assert sealed.path.exists() and gateway.requests == []
    assert load_unit_capability(home) is None


def test_it_refuses_a_home_init_has_not_initialized(
    tmp_path: Path, gateway_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    uninitialized = (tmp_path / "runner").resolve()
    with serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway:
        sealed = seal_bundle(
            gateway_home,
            tmp_path / "unit.bundle",
            endpoint=refused_endpoint(),
            unit_home=uninitialized,
        )

        rc = cmd_db_authority_install_unit(
            bundle=str(sealed.path), transport_key=sealed.transport_key, home=uninitialized
        )

    assert rc == 1
    assert "not initialized: run `meiva init` first" in capsys.readouterr().err
    assert sealed.path.exists() and gateway.requests == []


def test_it_needs_the_transport_key_and_keeps_the_bundle(
    tmp_path: Path, gateway_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway:
        home = initialized_runner(tmp_path, gateway_url=gateway.url)
        sealed = seal_bundle(
            gateway_home, tmp_path / "unit.bundle", endpoint=refused_endpoint(), unit_home=home
        )

        rc = cmd_db_authority_install_unit(bundle=str(sealed.path), transport_key="", home=home)

    assert rc == 1
    assert "MEIVA_DB_CAPABILITY_KEY" in capsys.readouterr().err
    assert sealed.path.exists() and gateway.requests == []
    assert load_unit_capability(home) is None


def test_it_refuses_another_units_bundle_and_keeps_it(
    tmp_path: Path, gateway_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serving({BOOTSTRAP: bootstrap_reply(refused_endpoint())}) as gateway:
        home = initialized_runner(tmp_path, gateway_url=gateway.url)
        sealed = seal_bundle(
            gateway_home,
            tmp_path / "other.bundle",
            endpoint=refused_endpoint(),
            unit_home=home,
            machine="elsewhere",
        )

        rc = cmd_db_authority_install_unit(
            bundle=str(sealed.path), transport_key=sealed.transport_key, home=home
        )

    assert rc == 1
    assert "was issued for" in capsys.readouterr().err
    assert sealed.path.exists() and load_unit_capability(home) is None
