from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.paths.tests.homes import point_home
from base.sessions.helperproc import helper_for
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_root_construction_does_not_prepare_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_prepare(_config: ConfigBoot) -> None:
        pytest.fail("constructing an invocation delivered configuration")

    monkeypatch.setattr(ConfigBoot, "prepare", unexpected_prepare)
    first = CommandRuntime.create()
    second = CommandRuntime.create()
    assert first.config is not second.config
    assert first.machine is not second.machine
    assert not first.config.prepared and not second.config.prepared


def test_reset_reloads_only_this_invocations_identity(command_runtime: CommandRuntime) -> None:
    other = CommandRuntime.create()
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


def test_the_runtime_runs_with_the_helper_its_hosts_platform_has() -> None:
    runtime = CommandRuntime.create()
    assert runtime.helper is helper_for(runtime.host.platform)
    assert (runtime.helper is None) == (runtime.host.platform.system == "linux")


# meiva:note
# The runtime names the operator's home in the one spelling `meiva init` and `meiva start`
# record (resolved through any symlink on the way), and only reads it: creating the private
# directory is the entry's business (`cli.main`), so building a runtime makes nothing.
def test_the_runtime_names_the_resolved_home_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    point_home(monkeypatch, link / "home")

    runtime = CommandRuntime.create()

    assert runtime.home == real / "home"
    assert not runtime.home.exists()


def test_the_runtime_holds_the_session_of_the_user_who_ran_it() -> None:
    session = CommandRuntime.create().session
    assert session.user_home == Path.home()
