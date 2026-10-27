# meiva:note
# A sealed database capability from a gateway home to a runner, on a real single-box cluster.
#
# The gateway home is a born cluster (real Postgres behind PgBouncer, a private ledger with an
# active write generation); the gateway the runner joins is a scripted listener on a loopback
# port that serves the configuration the real bootstrap builder produces for that home. The
# bundle `issue-unit` seals is opened by `meiva init --db-capability`, renewed by `install-unit`,
# and the runner then connects as the generation's login. What each verb refuses and says is
# asserted in `cli/commands/cluster/tests/unit_capability`.

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from dotenv import dotenv_values

from base import config
from base.cluster.authority.tests.unit_capability_support import HUMAN, MACHINE
from base.config.service_read import ConfigAuthority
from base.host.db_authority import delivery, unit
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.config_registration import plugin_bootstrap_config
from base.paths import meiva_home
from cli.commands.cluster.control import cmd_db_authority_install_unit, cmd_db_authority_issue_unit
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.commands.data_plane import bringup
from cli.commands.data_plane.instance.launch_delivery import LaunchDelivery
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.tests import test_single_box as _single_box
from cli.commands.tests.test_single_box import Born
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.start_runtime import HomeSite
from cli.tests.bootstrap.init_identity import caller_host, prepare_init_identity

configured = _single_box.configured
born = _single_box.born


# meiva:note
# The gateway operator's invocation: the born home, the human secret the bundles derive their
# telemetry token from, and `localhost` as the address runners reach the gateway on. The home
# serves the gateway through its `.env`, as `meiva init` leaves it.
def _gateway_operator(command_runtime: CommandRuntime, born: Born) -> CommandRuntime:
    with (born.home / ".env").open("a") as env_file:
        env_file.write("MEIVA_MACHINE_SERVE_GATEWAY=true\n")
    command_runtime.config.set_field("cluster_secret", HUMAN)
    command_runtime.config.set_field("machine_host", "localhost")
    return replace(command_runtime, home=born.home)


def _runner_args(bundle: Path, gateway_url: str) -> Any:
    from cli.parsers import build_parser

    return build_parser().parse_args(
        [
            "init",
            "--serve-agent-runner",
            "--no-serve-gateway",
            "--machine-name",
            MACHINE,
            "--gateway-url",
            gateway_url,
            "--db-capability",
            str(bundle),
        ]
    )


def _issue_bundle(
    command: CommandRuntime, out: Path, runner: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str]:
    assert (
        cmd_db_authority_issue_unit(
            machine=MACHINE, home=str(runner), out=str(out), ttl_hours=1, command=command
        )
        == 0
    )
    printed = capsys.readouterr().out
    match = re.search(r"transport key \(shown once, carry it separately\): (\S+)", printed)
    assert match is not None, printed
    return printed, match.group(1)


def test_issued_bundle_starts_a_runner_that_connects_as_the_generation_login(
    born: Born,
    command_runtime: CommandRuntime,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _gateway_operator(command_runtime, born)
    runner = (tmp_path / "runner").resolve()
    bundle = tmp_path / "mini.bundle"
    printed, key = _issue_bundle(command, bundle, runner, capsys)
    renewal = tmp_path / "mini-renewal.bundle"
    _, renewal_key = _issue_bundle(command, renewal, runner, capsys)
    assert "`.venv/bin/meiva init --db-capability <bundle>`" in printed, printed
    assert "`meiva cluster db-authority install-unit <bundle>`" in printed, printed
    assert bundle.stat().st_mode & 0o777 == 0o600
    served = config.bootstrap_config_values(
        ConfigAuthority.deferred(
            runtime=command.config.view,
            build_all_domains=command.config.complete_read_model,
            env_path=born.home / ".env",
        ),
        provider_key_envs=(
            binding.key_env for binding in build_model_catalog(home=meiva_home()).bindings.values()
        ),
        plugin_cluster_config=plugin_bootstrap_config(home=meiva_home()),
    )
    assert urlsplit(served["MEIVA_DB_URL"]).password is None
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with serving({("GET", "/api/bootstrap"): Reply(body=served)}) as gateway:
        prepare_init_identity(
            _runner_args(bundle, gateway.url),
            HomeSite(runner, checkout),
            replace(caller_host(path=str(tmp_path / "tools")), transport_key=key),
        )
        assert (
            cmd_db_authority_install_unit(
                bundle=str(renewal), transport_key=renewal_key, home=runner
            )
            == 0
        )
    assert not bundle.exists() and not renewal.exists()
    env = dotenv_values(runner / ".env")
    assert "MEIVA_DB_URL" not in env and unit.CAPABILITY_KEY_ENV not in env
    assert "MEIVA_CLUSTER_SECRET" not in env

    capability = unit.require_unit_capability(runner)
    assert [seen.headers["authorization"] for seen in gateway.requests] == [
        f"Bearer {capability.api.token}"
    ] * 2
    with psycopg.connect(capability.dsn, prepare_threshold=None, connect_timeout=5) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("meiva_g0_runner",)
        assert conn.execute("SELECT count(*) FROM agents_meta").fetchone() is not None
    launch = LaunchDelivery(home=runner, remote_data_plane=False, environment={}, env_file={})
    assert bringup.db_delivery("runner", delivery=launch) == {
        "MEIVA_DB_URL": capability.dsn,
        delivery.GENERATION_ENV: "0",
    }
