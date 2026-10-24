import sys
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.paths import repo_root
from base.sessions.helperproc import helper_for
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_root_construction_does_not_prepare_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_prepare(_config: ConfigBoot) -> None:
        pytest.fail("constructing an invocation delivered configuration")

    monkeypatch.setattr(ConfigBoot, "prepare", unexpected_prepare)
    first = CommandRuntime.create(tmp_path)
    second = CommandRuntime.create(tmp_path)
    assert first.config is not second.config
    assert first.machine is not second.machine
    assert not first.config.prepared and not second.config.prepared


def test_reset_reloads_only_this_invocations_identity(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    other = CommandRuntime.create(tmp_path)
    other.config.set_field("machine_name", "other-root")
    command_runtime.config.set_field("machine_name", "first-name")
    assert command_runtime.machine.name() == "first-name"
    assert other.machine.name() == "other-root"

    command_runtime.config.set_field("machine_name", "repaired-name")
    assert command_runtime.machine.name() == "first-name"
    command_runtime.machine.reset()
    assert command_runtime.machine.name() == "repaired-name"
    assert other.machine.name() == "other-root"


def test_narrow_machine_fixture_borrows_the_invocation_owner(
    command_runtime: CommandRuntime, command_machine: MachineIdentity
) -> None:
    assert command_machine is command_runtime.machine


def test_the_runtime_runs_with_the_helper_its_hosts_platform_has(tmp_path: Path) -> None:
    runtime = CommandRuntime.create(tmp_path)
    assert runtime.helper is helper_for(runtime.host.platform)
    assert (runtime.helper is None) == (runtime.host.platform.system == "linux")


# meiva:note
# The runtime holds the home the entry resolved and only that: it neither resolves a spelling nor
# creates the private directory (`cli.main` does both once), so building a runtime makes nothing.
def test_the_runtime_holds_the_home_it_is_handed_and_creates_nothing(tmp_path: Path) -> None:
    home = tmp_path.resolve() / "home"

    runtime = CommandRuntime.create(home)

    assert runtime.home == home
    assert not home.exists()


def test_the_runtime_holds_the_session_the_checkout_and_the_terminal_of_the_process(
    tmp_path: Path,
) -> None:
    runtime = CommandRuntime.create(tmp_path)

    assert runtime.session.user_home == Path.home()
    assert runtime.checkout == repo_root()
    assert runtime.terminal.stdout is sys.stdout
