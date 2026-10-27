# meiva:note
# The converge context the step tests start from.
#
# `ctx` is a configured agent-runner on a Linux host that finds no program and runs no command,
# whose user session is a directory under `tmp_path`. A test changes what it cares about with
# `dataclasses.replace(ctx, ...)` (the host, the roles, the session) and sets configuration on
# `ctx.config`; nothing here patches an environment variable or an attribute.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime, UserSession

__all__ = ["capable_mac", "converge_context", "ctx", "user_home"]


@pytest.fixture
def user_home(tmp_path: Path) -> Path:
    """The invoking user's home directory, which a test creates when it needs it to exist.

    It is the directory `default_home` makes the fake OS user's home, so a test that needs
    both finds one user."""
    return tmp_path / "user"


# meiva:note
# A configured agent-runner context over `command_runtime`'s owners and inert host.
def converge_context(
    repo: Path,
    meiva_home: Path,
    *,
    user_home: Path,
    command_runtime: CommandRuntime,
    database_factory: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> ConvergeCtx:
    return ConvergeCtx(
        repo=repo,
        meiva_home=meiva_home,
        roles=frozenset({"agent-runner"}),
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=database_factory,
        producer=producer,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=user_home, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=frozenset(),
    )


@pytest.fixture
def ctx(
    tmp_path: Path,
    user_home: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> ConvergeCtx:
    return converge_context(
        tmp_path / "repo",
        tmp_path / "meiva-home",
        user_home=user_home,
        command_runtime=command_runtime,
        database_factory=operator_database,
        producer=operator_pipeline,
    )


# meiva:note
# A macOS host that can run the permissions helper: it has the Xcode tools it needs.
def capable_mac(*, programs: dict[str, str] | None = None) -> Host:
    found = {"swiftc": "/usr/bin/swiftc", "codesign": "/usr/bin/codesign", **(programs or {})}
    return fake_host(platform=DARWIN_ARM64, programs=found)
