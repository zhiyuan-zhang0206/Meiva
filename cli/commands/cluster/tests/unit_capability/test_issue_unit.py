# meiva:note
# `meiva cluster db-authority issue-unit`, end to end against a real gateway home.
#
# The home is a directory on disk with a private ledger that holds an active write generation
# and a `.env` that names its database; the invocation's configuration (the cluster secret, the
# reachable address, the data plane) is the `CommandRuntime` the command is handed. Which homes
# the verb refuses and the wording of the report are asserted on values in
# `test_db_authority_replies.py`; these tests prove the shell reads the home and the
# configuration it was given, seals a bundle only the printed key opens, and writes nothing
# when it refuses.

from __future__ import annotations

import re
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from base.host.db_authority.api import telemetry_token
from base.host.db_authority.unit import UnitIdentity, open_bundle
from cli.commands.cluster.control import cmd_db_authority_issue_unit
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    CLUSTER_SECRET,
    MACHINE,
    runner_api_token,
)
from cli.commands.cluster.tests.unit_capability.capability_harness import (
    gateway_home as gateway_home,
)
from cli.database.runtime import CommandRuntime

_LOCAL_DATABASE = "postgresql://meiva@127.0.0.1:6433/meiva"
_REACHABLE = "10.0.0.7"


# meiva:note
# The invocation of a gateway operator: the gateway home on disk, serving the gateway and
# naming its database in its `.env`, and the configuration that names the secret and the address
# runners reach it on.
def _operator(
    command_runtime: CommandRuntime, home: Path, *, serves_gateway: bool
) -> CommandRuntime:
    flag = "true" if serves_gateway else "false"
    (home / ".env").write_text(
        f"MEIVA_MACHINE_SERVE_GATEWAY={flag}\nMEIVA_DB_URL={_LOCAL_DATABASE}\n"
    )
    command_runtime.config.set_field("cluster_secret", CLUSTER_SECRET)
    command_runtime.config.set_field("machine_host", _REACHABLE)
    return replace(command_runtime, home=home)


def _issue(command: CommandRuntime, out: Path, *, unit_home: str = "/home/u/.meiva") -> int:
    return cmd_db_authority_issue_unit(
        machine=MACHINE, home=unit_home, out=str(out), ttl_hours=1, command=command
    )


def _printed_key(printed: str) -> str:
    match = re.search(r"transport key \(shown once, carry it separately\): (\S+)", printed)
    assert match is not None, printed
    return match.group(1)


def test_it_seals_the_active_generation_for_one_unit_under_the_key_it_prints(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    out = tmp_path / "mini.bundle"

    assert _issue(command, out) == 0

    printed = capsys.readouterr().out
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert f"written to {out} (0600)" in printed
    assert "`meiva cluster db-authority install-unit <bundle>`" in printed
    bundle = open_bundle(out.read_bytes(), _printed_key(printed))
    capability = bundle.capability
    assert capability.unit == UnitIdentity(machine=MACHINE, home="/home/u/.meiva")
    assert capability.endpoint == f"postgresql://meiva@{_REACHABLE}:6433/meiva"
    assert capability.generation.number == 0
    assert capability.api.token == runner_api_token(gateway_home)
    assert capability.api.telemetry == telemetry_token(CLUSTER_SECRET)
    assert bundle.gateway_home == str(gateway_home)


def test_it_refuses_a_home_that_does_not_serve_the_gateway_and_writes_nothing(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=False)
    out = tmp_path / "mini.bundle"

    assert _issue(command, out) == 1

    captured = capsys.readouterr()
    assert "runs on a gateway home with a local data plane" in captured.err
    assert captured.out == "" and not out.exists()


def test_it_refuses_a_remote_data_plane_and_writes_nothing(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    command.config.set_field("db_url", "postgresql://meiva@10.0.0.9:5432/meiva")
    out = tmp_path / "mini.bundle"

    assert _issue(command, out) == 1

    assert "a remote-managed plane has no write generation to issue" in capsys.readouterr().err
    assert not out.exists()


def test_it_never_overwrites_a_bundle_that_is_already_there(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    out = tmp_path / "mini.bundle"
    out.write_bytes(b"an earlier bundle")

    assert _issue(command, out) == 1

    assert "issue-unit:" in capsys.readouterr().err
    assert out.read_bytes() == b"an earlier bundle"


def test_it_refuses_a_lifetime_beyond_what_a_bundle_may_ask_and_writes_nothing(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    out = tmp_path / "mini.bundle"

    rc = cmd_db_authority_issue_unit(
        machine=MACHINE, home="/home/u/.meiva", out=str(out), ttl_hours=1000, command=command
    )

    assert rc == 1
    assert "lifetime must be positive and at most" in capsys.readouterr().err
    assert not out.exists()


def test_it_refuses_a_unit_home_that_is_not_an_absolute_path(
    tmp_path: Path,
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    out = tmp_path / "mini.bundle"

    assert _issue(command, out, unit_home="relative/home") == 1

    assert "absolute path" in capsys.readouterr().err
    assert not out.exists()


def test_it_refuses_a_gateway_home_with_no_active_generation(
    tmp_path: Path, command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    unborn = (tmp_path / "unborn").resolve()
    unborn.mkdir(mode=0o700)
    command = _operator(command_runtime, unborn, serves_gateway=True)
    out = tmp_path / "mini.bundle"

    assert _issue(command, out) == 1

    assert "issue-unit:" in capsys.readouterr().err
    assert not out.exists()


# meiva:note
# `--out` may start with `~`: it names the home the invocation read (the operator's session), not
# the process's, so the bundle lands under that home even when the process runs elsewhere.
def test_a_tilde_in_the_output_path_expands_against_the_invocations_home(
    gateway_home: Path,
    command_runtime: CommandRuntime,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "operator"
    user_home.mkdir()
    command = _operator(command_runtime, gateway_home, serves_gateway=True)
    command = replace(command, session=replace(command.session, user_home=user_home))

    assert _issue(command, Path("~/mini.bundle")) == 0

    assert (user_home / "mini.bundle").exists()


# meiva:note
# `~someone/...` names another account's home, which an invocation does not read: the command says
# so and writes nothing instead of expanding it against the process's user database.
def test_another_users_home_in_the_output_path_is_refused(
    gateway_home: Path,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _operator(command_runtime, gateway_home, serves_gateway=True)

    assert _issue(command, Path("~someone/mini.bundle")) == 1

    assert "names another user's home" in capsys.readouterr().err
