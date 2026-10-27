# meiva:note
# The host-dependency gate against real executables.
#
# Each test builds a directory of tiny real programs and a `Host` whose `which` searches only that
# directory, so what the host "has" is exactly what the test wrote. The gate reads it with real
# `Host.which` and `Host.run` calls, installs a missing group by running a real shell script from
# a tmp checkout's `scripts/provision/`, and reads the host again. The systemd row reads the
# machine's own service manager, so no test here selects the boot unit.

from __future__ import annotations

import dataclasses
import shutil
import stat
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from base.cluster.dataplane import runtime_binaries
from base.config import ConfigBoot
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86
from base.sessions.env_forwarding import frontend_toolchain_path
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.host_dependencies import (
    Probing,
    ensure_host_dependencies,
    ensure_host_dependencies_step,
    probing_for,
    scope_for,
)
from cli.commands.data_plane.host_dependencies.scope import PlaneFacts, StartScope
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

RUNNER = frozenset({"agent-runner"})
GATEWAY = frozenset({"gateway"})


def _program(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _host(directory: Path, *, macos: bool = False, uid: int = 1000) -> Host:
    return Host(
        run=run_bounded,
        which=partial(shutil.which, path=str(directory)),
        platform=DARWIN_ARM64 if macos else LINUX_X86,
        uid=uid,
    )


def _scope(
    *,
    roles: frozenset[str] = RUNNER,
    services: frozenset[str] = frozenset(),
    data_plane: PlaneFacts | None = None,
    macos: bool = False,
    os_jobs: bool = True,
) -> StartScope:
    return StartScope(
        roles=roles,
        services=services,
        data_plane=data_plane,
        macos=macos,
        is_root=False,
        os_jobs=os_jobs,
        permissions_helper=False,
        require_github_pr=False,
        memory_keep_local=False,
        prod_install=False,
    )


# meiva:note
# A real provisioning script in the checkout; `body` is its shell text.
def _provision(repo: Path, script: str, body: str) -> Path:
    return _program(repo / "scripts" / "provision" / script, body)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """The only directory the test host searches: a runner's `git` and `crontab`."""
    directory = tmp_path / "bin"
    for name in ("git", "crontab"):
        _program(directory / name)
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


def _gate(repo: Path, scope: StartScope, host: Host, directory: Path) -> int:
    return ensure_host_dependencies(
        repo=repo,
        scope=scope,
        probing=Probing(host=host, data_plane=None, toolchain_path=str(directory)),
    )


def test_all_present_is_silent(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _gate(repo, _scope(), _host(bin_dir), bin_dir) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_missing_tool_installs_its_group_then_passes_and_is_not_reinstalled(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bin_dir / "crontab").unlink()
    runs = repo / "installs.log"
    repo.mkdir()
    _provision(
        repo,
        "host_utils.sh",
        f"echo run >> '{runs}'\nprintf '#!/bin/sh\\nexit 0\\n' > '{bin_dir}/crontab'\n"
        f"chmod +x '{bin_dir}/crontab'",
    )
    host = _host(bin_dir)

    assert _gate(repo, _scope(), host, bin_dir) == 0
    first = capsys.readouterr()
    assert "· crontab: not on PATH" in first.out
    assert "installing cron + curl with apt" in first.out
    assert "✓ host dependencies ready" in first.out
    assert (bin_dir / "crontab").is_file()

    assert _gate(repo, _scope(), host, bin_dir) == 0
    assert "host dependencies" not in capsys.readouterr().out
    assert runs.read_text().splitlines() == ["run"], "the second start must not reinstall"


def test_a_group_whose_script_leaves_the_tool_missing_refuses_with_its_command(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bin_dir / "crontab").unlink()
    repo.mkdir()
    _provision(repo, "host_utils.sh", "exit 3")

    assert _gate(repo, _scope(), _host(bin_dir), bin_dir) == 1

    err = capsys.readouterr().err
    assert "host_utils.sh exited 3; re-checking what landed" in err
    assert "launched nothing" in err
    assert "✗ crontab" in err
    assert str(repo / "scripts/provision/host_utils.sh") in err


def test_a_group_without_its_script_in_the_checkout_refuses(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bin_dir / "crontab").unlink()

    assert _gate(repo, _scope(), _host(bin_dir), bin_dir) == 1

    err = capsys.readouterr().err
    assert f"provision script missing: {repo}/scripts/provision/host_utils.sh" in err
    assert "✗ crontab" in err


def test_a_host_owned_tool_refuses_without_running_any_installer(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bin_dir / "git").unlink()
    repo.mkdir()
    runs = repo / "installs.log"
    _provision(repo, "install-cli-tools.sh", f"echo run >> '{runs}'")
    scope = _scope(macos=True)

    assert _gate(repo, scope, _host(bin_dir, macos=True), bin_dir) == 1

    err = capsys.readouterr().err
    assert "✗ launchctl" in err and "launchd ships with macOS" in err
    assert "✗ git" in err
    assert not runs.exists(), "a refusal that is certain must not spend minutes installing"


# meiva:note
# The version probe runs the found `node`; an older one is replaced by the group's install.
def test_node_is_read_by_running_it_and_an_install_that_replaces_it_passes(
    bin_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.mkdir()
    for name in ("npm", "npx", "curl"):
        _program(bin_dir / name)
    old = _program(bin_dir / "node", 'echo "18.19.1"')
    _provision(
        repo,
        "node.sh",
        f"printf '#!/bin/sh\\necho 22.14.0\\n' > '{old}'\nchmod +x '{old}'",
    )
    scope = _scope(roles=RUNNER, services=frozenset({"frontend"}), os_jobs=False)

    assert _gate(repo, scope, _host(bin_dir), bin_dir) == 0

    out = capsys.readouterr().out
    assert "· node: 18.19.1 is older than 20.9" in out
    assert "installing Node.js with apt" in out


def test_a_node_toolchain_outside_the_inherited_path_is_found_through_the_toolchain_path(
    bin_dir: Path, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toolchain = tmp_path / "nvm"
    for name in ("npm", "npx"):
        _program(toolchain / name)
    _program(toolchain / "node", 'echo "22.14.0"')
    _program(bin_dir / "curl")
    scope = _scope(services=frozenset({"frontend"}), os_jobs=False)
    host = _host(bin_dir)
    probing = Probing(host=host, data_plane=None, toolchain_path=f"{toolchain}:{bin_dir}")

    assert ensure_host_dependencies(repo=repo, scope=scope, probing=probing) == 0
    assert capsys.readouterr().out == ""


def test_the_database_tools_are_read_from_the_vendored_tree_and_the_redis_override(
    unit_home: Path,
    bin_dir: Path,
    tmp_path: Path,
    repo: Path,
    data_plane_config: DataPlaneConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vendored = runtime_binaries.vendored_pg_dir() / "bin"
    assert vendored.is_relative_to(unit_home)
    for name in ("initdb", "pg_ctl", "postgres", "pg_isready"):
        _program(vendored / name)
    redis_dir = tmp_path / "redis-bin"
    for name in ("redis-server", "redis-cli"):
        _program(redis_dir / name)
    plane = dataclasses.replace(
        data_plane_config, redis_bin_dir=str(redis_dir), pgbouncer_enabled=False
    )
    scope = _scope(
        roles=GATEWAY,
        data_plane=PlaneFacts(is_remote=False, pgbouncer_enabled=False),
        os_jobs=False,
    )
    probing = Probing(host=_host(bin_dir), data_plane=plane, toolchain_path=str(bin_dir))

    assert ensure_host_dependencies(repo=repo, scope=scope, probing=probing) == 0
    assert capsys.readouterr().out == ""

    (redis_dir / "redis-cli").chmod(0o644)
    runs = repo / "installs.log"
    repo.mkdir()
    _provision(repo, "database.sh", f"echo run >> '{runs}'")

    assert ensure_host_dependencies(repo=repo, scope=scope, probing=probing) == 1

    err = capsys.readouterr().err
    assert "MEIVA_REDIS_BIN_DIR is unusable" in err
    assert not runs.exists(), "an unusable override is not something apt can fix"


def _context(
    tmp_path: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    host: Host,
    roles: frozenset[str] = RUNNER,
    services: frozenset[str] = frozenset({"frontend"}),
) -> ConvergeCtx:
    return ConvergeCtx(
        repo=Path("/repo"),
        meiva_home=Path.home() / ".meiva",
        roles=roles,
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=operator_database,
        producer=operator_pipeline,
        helper=refuse_helper,
        host=host,
        session=UserSession(user_home=tmp_path, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
        services=services,
    )


# meiva:note
# OS jobs need the default home this process does not own; the helper flag reaches the scope.
def test_the_scope_is_read_from_the_context_the_gates_it_derives_included(
    unit_home: Path,
    tmp_path: Path,
    bin_dir: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    config: ConfigBoot = command_runtime.config
    config.set_field("os_jobs_enabled", True)
    config.set_field("permissions_helper_enabled", True)
    config.set_field("require_github_pr", True)
    config.set_field("memory_keep_local", False)
    ctx = _context(
        tmp_path,
        command_runtime,
        operator_database,
        operator_pipeline,
        host=_host(bin_dir, macos=True, uid=0),
    )

    scope = scope_for(ctx)

    assert scope == StartScope(
        roles=RUNNER,
        services=frozenset({"frontend"}),
        data_plane=None,
        macos=True,
        is_root=True,
        os_jobs=False,
        permissions_helper=True,
        require_github_pr=True,
        memory_keep_local=False,
        prod_install=True,
    )


def test_a_gateway_context_reads_its_data_plane_facts_and_the_toolchain_path(
    unit_home: Path,
    tmp_path: Path,
    bin_dir: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    command_runtime.config.set_field("pgbouncer_enabled", False)
    ctx = _context(
        tmp_path,
        command_runtime,
        operator_database,
        operator_pipeline,
        host=_host(bin_dir),
        roles=GATEWAY,
    )

    scope = scope_for(ctx)
    probing = probing_for(ctx)

    assert probing.data_plane is not None
    assert scope.data_plane == PlaneFacts(
        is_remote=probing.data_plane.is_remote, pgbouncer_enabled=False
    )
    assert probing.host is ctx.host
    inherited = command_runtime.config.process_environment().get("PATH", "")
    assert probing.toolchain_path == frontend_toolchain_path(inherited)


def test_the_step_raises_when_the_host_cannot_satisfy_the_start(
    unit_home: Path,
    tmp_path: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    ctx = _context(
        tmp_path,
        command_runtime,
        operator_database,
        operator_pipeline,
        host=_host(empty),
        services=frozenset(),
    )

    with pytest.raises(RuntimeError, match="refused this start"):
        ensure_host_dependencies_step(ctx)

    assert "✗ git" in capsys.readouterr().err


def test_the_step_passes_when_every_required_tool_is_present(
    unit_home: Path,
    tmp_path: Path,
    bin_dir: Path,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    command_runtime.config.set_field("require_github_pr", False)
    ctx = _context(
        tmp_path,
        command_runtime,
        operator_database,
        operator_pipeline,
        host=_host(bin_dir),
        services=frozenset(),
    )

    ensure_host_dependencies_step(ctx)

    assert capsys.readouterr().out == ""
