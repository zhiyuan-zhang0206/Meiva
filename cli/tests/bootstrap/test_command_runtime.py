import sys
from pathlib import Path

from base.cluster.machine import MachineIdentityReader
from base.host.proc import run_bounded
from base.paths import repo_root
from base.sessions.helperproc import helper_for
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


# meiva:note
# The child's local-source home cannot prepare configuration: construction must still be lazy.
def test_root_construction_does_not_prepare_configuration(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    user_home.mkdir()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    env_file = home / ".env"
    env_file.write_text("MEIVA_MACHINE_SERVE_GATEWAY=true\n")
    before = env_file.read_bytes()

    child = run_bounded(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "cli.tests.guards.command_runtime_child",
            str(home),
        ],
        cwd=user_home,
        env={
            "HOME": str(user_home),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert child.returncode == 0, child.stderr
    assert env_file.read_bytes() == before
    assert sorted(path.name for path in home.iterdir()) == [".env"]


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
    command_runtime: CommandRuntime, command_machine: MachineIdentityReader
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
