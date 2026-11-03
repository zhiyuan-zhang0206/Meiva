from __future__ import annotations

import random
from pathlib import Path
from typing import NoReturn

import pytest

from base.config import ConfigBoot
from base.host.env.harness.catalog import CATALOG
from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, Reply, fake_host
from base.telemetry import EventPipeline
from cli.commands.converge.harness_clis import ensure_external_harness_clis
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _never_dialed() -> NoReturn:
    raise AssertionError("this step must not open the database")


def _never_produced() -> EventPipeline:
    raise AssertionError("this step must not emit events")


@pytest.fixture
def ctx(tmp_path: Path, command_runtime: CommandRuntime) -> ConvergeCtx:
    user_home = (tmp_path / "user").resolve()
    user_home.mkdir(parents=True)
    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir(parents=True)

    fc = FakeCommands()
    for h in CATALOG.values():
        fc.on(
            lambda argv, b=h.binary: b in " ".join(argv) and "--version" in " ".join(argv),
            Reply(stdout=f"{h.cli} {h.pinned_version}\n", returncode=0),
            repeat=True,
        )

    isolated_host = fake_host(
        commands=fc,
        programs={},
        environ={"PATH": ""},
        platform=DARWIN_ARM64,
    )

    config = ConfigBoot(
        environment={"MEIVA_HOME": str(meiva_home), "MEIVA_CONFIG_FETCH": "skip", "PATH": ""}
    )

    return ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=tmp_path / "repo",
        meiva_home=meiva_home,
        roles=frozenset({"agent-runner"}),
        config=config,
        machine=command_runtime.machine,
        database_factory=_never_dialed,
        producer=_never_produced,
        helper=command_runtime.helper,
        host=isolated_host,
        session=UserSession(user_home=user_home, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=frozenset(),
        terminal=command_runtime.terminal,
    )


def test_ensure_external_harness_clis_succeeds_when_clean(ctx: ConvergeCtx) -> None:
    ensure_external_harness_clis(ctx)


def test_ensure_external_harness_clis_raises_on_failure(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    user_home = (tmp_path / "user").resolve()
    user_home.mkdir(parents=True)
    meiva_home = (tmp_path / "meiva").resolve()
    meiva_home.mkdir(parents=True)

    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    claude_bin = bin_dir / "claude"
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)

    fc = FakeCommands()
    fc.on(
        lambda argv: "claude" in " ".join(argv) and "--version" in " ".join(argv),
        Reply(stdout="claude 0.0.1\n", returncode=0),
        repeat=True,
    )
    for h in CATALOG.values():
        if h.cli != "claude":
            fc.on(
                lambda argv, b=h.binary: b in " ".join(argv) and "--version" in " ".join(argv),
                Reply(stdout=f"{h.cli} {h.pinned_version}\n", returncode=0),
                repeat=True,
            )
    fc.on(
        lambda argv: "install.sh" in " ".join(argv) or "install" in " ".join(argv),
        Reply(stderr="install failed", returncode=1),
        repeat=True,
    )

    isolated_host = fake_host(
        commands=fc,
        programs={},
        environ={"PATH": ""},
        platform=DARWIN_ARM64,
    )

    config = ConfigBoot(
        environment={"MEIVA_HOME": str(meiva_home), "MEIVA_CONFIG_FETCH": "skip", "PATH": ""}
    )
    fail_ctx = ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=tmp_path / "repo",
        meiva_home=meiva_home,
        roles=frozenset({"agent-runner"}),
        config=config,
        machine=command_runtime.machine,
        database_factory=_never_dialed,
        producer=_never_produced,
        helper=command_runtime.helper,
        host=isolated_host,
        session=UserSession(user_home=user_home, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=frozenset(),
        terminal=command_runtime.terminal,
    )

    with pytest.raises(RuntimeError, match="external harness CLI convergence failed"):
        ensure_external_harness_clis(fail_ctx)
