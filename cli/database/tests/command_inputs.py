"""Explicit configuration and machine inputs for one CLI test invocation."""

import io
import os
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home, repo_root
from base.sessions.tests.refuse_helper import refuse_helper
from cli.database.runtime import CommandRuntime, Terminal, UserSession

__all__ = ["command_config_machine", "command_machine", "command_runtime"]


@pytest.fixture
def command_runtime(tmp_path: Path) -> CommandRuntime:
    """Own function-local config overlays and identity caches for this invocation.

    The host is inert: it finds no program and runs no command, so a test that reaches for the
    host must say what the host answers (`fake_host(...)`). The user's session is a zsh login
    with no terminal whose home is `tmp_path / "user"`, so a step that wires the user's account
    writes under the test's own directory. The terminal is three empty streams that are not
    ttys, so a command that asks a person a question finds nobody to answer.
    """
    config = snapshot_process_config()
    return CommandRuntime(
        config=config,
        pid=os.getpid(),
        home=meiva_home(),
        machine=ConfiguredMachineIdentity(config),
        helper=refuse_helper,
        host=fake_host(),
        session=UserSession(user_home=tmp_path / "user", login_shell="/bin/zsh", interactive=False),
        timers=ManualTimers(),
        rng=seeded_rng(),
        checkout=repo_root(),
        terminal=Terminal(io.StringIO(), io.StringIO(), io.StringIO()),
    )


@pytest.fixture
def command_machine(command_runtime: CommandRuntime) -> ConfiguredMachineIdentity:
    """Borrow only the machine capability from this invocation's existing owner."""
    return command_runtime.machine


@pytest.fixture
def command_config_machine(
    command_runtime: CommandRuntime,
) -> tuple[ConfigBoot, ConfiguredMachineIdentity]:
    """Borrow both capabilities together from the same invocation owner."""
    return command_runtime.config, command_runtime.machine
