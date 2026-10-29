from __future__ import annotations

import os
import random
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.agents.context.clients import DatabaseFactory
from base.cluster.dataplane import pg_runtime
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.converge.steps import ensure_pg_binaries_step
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    bindir = tmp_path / "pg17" / "bin"
    bindir.mkdir(parents=True)
    extension = bindir.parent / "share" / "extension"
    extension.mkdir(parents=True)
    library = bindir.parent / "lib"
    library.mkdir()
    (extension / "vector.control").write_text("default_version = '0.8.6'\n")
    (extension / "vector--0.8.6.sql").write_text("-- fixture\n")
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    (library / f"vector{suffix}").touch()
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        ' --version) echo "PostgreSQL 17.11";;\n'
        f" --bindir) echo {shlex.quote(str(bindir))};;\n"
        f" --sharedir) echo {shlex.quote(str(extension.parent))};;\n"
        f" --pkglibdir) echo {shlex.quote(str(library))};;\n"
        " *) exit 3;;\nesac\n"
    )
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o700)
    return bindir


def test_start_converge_accepts_installed_pg17_where_no_artifact_exists(
    installed: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ensure_pg_binaries_step(
        ConvergeCtx(
            installed.parent,
            installed.parent,
            frozenset({"gateway"}),
            secure_rng=random.SystemRandom(),
            config=command_runtime.config,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
            host=fake_host(uid=os.geteuid()),
            session=UserSession(
                user_home=installed.parent, login_shell="/bin/zsh", interactive=False
            ),
            services=frozenset(),
            timers=command_runtime.timers,
            clock=command_runtime.clock,
            terminal=command_runtime.terminal,
        ),
        system="FreeBSD",
        machine="riscv64",
        platform_bin_dir=installed,
        path_env="",
    )
    assert (
        pg_runtime.pg_tool(
            "postgres",
            home=installed.parent,
            platform_bin_dir=installed,
            path_env="",
        )
        == installed / "postgres"
    )


def test_remote_managed_gateway_does_not_require_local_server_or_extension(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    config = command_runtime.config
    config.set_field("db_url", "postgresql://test:test@192.0.2.10:5433/meiva")
    config.set_field("redis_url", "redis://127.0.0.1:6380/0")
    ensure_pg_binaries_step(
        ConvergeCtx(
            tmp_path,
            tmp_path,
            frozenset({"gateway"}),
            secure_rng=random.SystemRandom(),
            config=config,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
            host=fake_host(uid=os.geteuid()),
            session=UserSession(user_home=tmp_path, login_shell="/bin/zsh", interactive=False),
            services=frozenset(),
            timers=command_runtime.timers,
            clock=command_runtime.clock,
            terminal=command_runtime.terminal,
        ),
        system="FreeBSD",
        machine="riscv64",
        platform_bin_dir=tmp_path / "missing",
        path_env="",
    )


def test_pg_admission_uses_each_owners_live_remote_verdict(
    tmp_path: Path,
    operator_database: DatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    missing = tmp_path / "missing"
    local, remote = ConfigBoot(), ConfigBoot()
    contexts: list[ConvergeCtx] = []
    for config, host in ((local, "127.0.0.1"), (remote, "192.0.2.10")):
        config.set_field("db_url", f"postgresql://test:test@{host}:5433/meiva")
        config.set_field("redis_url", "redis://127.0.0.1:6380/0")
        contexts.append(
            ConvergeCtx(
                tmp_path,
                tmp_path,
                frozenset({"gateway"}),
                secure_rng=random.SystemRandom(),
                config=config,
                database_factory=operator_database,
                producer=operator_pipeline,
                machine=ConfiguredMachineIdentity(config),
                helper=refuse_helper,
                host=fake_host(uid=os.geteuid()),
                session=UserSession(user_home=tmp_path, login_shell="/bin/zsh", interactive=False),
                services=frozenset(),
                timers=command_runtime.timers,
                clock=command_runtime.clock,
                terminal=command_runtime.terminal,
            )
        )
    with pytest.raises(RuntimeError, match="no vendored Postgres"):
        ensure_pg_binaries_step(
            contexts[0],
            system="FreeBSD",
            machine="riscv64",
            platform_bin_dir=missing,
            path_env="",
        )
    ensure_pg_binaries_step(
        contexts[1],
        system="FreeBSD",
        machine="riscv64",
        platform_bin_dir=missing,
        path_env="",
    )

    local.set_field("db_url", "postgresql://test:test@192.0.2.10:5433/meiva")
    remote.set_field("db_url", "postgresql://test:test@127.0.0.1:5433/meiva")
    ensure_pg_binaries_step(
        contexts[0],
        system="FreeBSD",
        machine="riscv64",
        platform_bin_dir=missing,
        path_env="",
    )
    with pytest.raises(RuntimeError, match="no vendored Postgres"):
        ensure_pg_binaries_step(
            contexts[1],
            system="FreeBSD",
            machine="riscv64",
            platform_bin_dir=missing,
            path_env="",
        )
