"""Explicit plugin scaffolding and its isolation from host convergence."""

import inspect
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from base import paths
from base.packages.plugins.enable_config import write_local
from base.paths.tests.homes import point_home
from base.telemetry import EventPipeline
from cli.commands.extensions import memory
from cli.commands.extensions.memory import run_plugin_scaffolds
from cli.commands.lifecycle import converge_host
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "meiva"
    home.mkdir()
    point_home(monkeypatch, home)


def _enable_user_plugins(plugins: dict[str, dict[str, object]]) -> None:
    write_local({"plugins": {"meiva_memory": {"enabled": False}, **plugins}})


# meiva:note
# A home patched only on base.paths leaves previously imported readers unchanged.
# Check the roster's actual configuration input against the isolated unit home.
def test_the_isolated_home_is_the_one_every_consumer_resolves(tmp_path: Path) -> None:
    from services.supervision.roster.composition import build_services

    home = tmp_path / "meiva"
    assert paths.meiva_home() == home
    collector = next(service for service in build_services() if service.session == "otel-collector")
    assert collector.config_inputs == (paths.otel_collector_config(home),)


def test_scaffold_runs_despite_dangling_config(*, command_runtime: CommandRuntime) -> None:
    pdir = paths.plugins_dir() / "real"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (pdir / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold(*, machine):\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('1')\n",
        encoding="utf-8",
    )
    _enable_user_plugins({"real": {"enabled": True}, "vanished": {"enabled": True}})

    result = run_plugin_scaffolds(machine=command_runtime.machine)  # must not raise

    assert result.ran == ["real"]
    assert (pdir / "scaffolded.marker").exists()


def test_broken_setup_py_is_skipped_and_other_scaffolds_run(
    loguru_records: list[dict[str, Any]],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A `setup.py` that fails to import is skipped loudly; the remaining
    plugins still scaffold (fail-soft, user ruling 2026-09-11) — one broken
    plugin must not block the provisioning command for every other plugin."""
    broken = paths.plugins_dir() / "broken"
    broken.mkdir(parents=True)
    (broken / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (broken / "setup.py").write_text("raise RuntimeError('setup boom')\n", encoding="utf-8")
    real = paths.plugins_dir() / "real"
    real.mkdir(parents=True)
    (real / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (real / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold(*, machine):\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('1')\n",
        encoding="utf-8",
    )
    _enable_user_plugins({"broken": {"enabled": True}, "real": {"enabled": True}})

    result = run_plugin_scaffolds(machine=command_runtime.machine)  # must not raise

    assert result.ran == ["real"]
    assert (real / "scaffolded.marker").exists()
    assert any(
        "broken" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_scaffold_execution_stays_fail_fast(*, command_runtime: CommandRuntime) -> None:
    """Only the `setup.py` IMPORT is fail-soft. A `scaffold()` that raises
    while RUNNING stops provisioning — the documented boundary stays pinned so
    a silent per-plugin skip cannot creep in (user ruling 2026-09-11:
    containment is for load failures, not execution)."""
    pdir = paths.plugins_dir() / "real"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (pdir / "setup.py").write_text(
        "def scaffold(*, machine):\n    raise RuntimeError('scaffold boom')\n", encoding="utf-8"
    )
    _enable_user_plugins({"real": {"enabled": True}})

    with pytest.raises(RuntimeError, match="scaffold boom"):
        run_plugin_scaffolds(machine=command_runtime.machine)


def test_converge_steps_do_not_scaffold_plugins() -> None:
    """Changing this back would reintroduce memory-repository Git work to start."""
    assert "plugin scaffolds" not in {step.name for step in converge_host.CONVERGE_STEPS}

    references = {
        step.name: inspect.getsource(step.apply)
        for step in converge_host.CONVERGE_STEPS
        if "run_plugin_scaffolds" in inspect.getsource(step.apply)
        or "scaffold" in inspect.getsource(step.apply)
    }
    assert references == {}


_HOST_INTEGRATION_STEP_NAMES = frozenset(
    {
        "health preflight",
        "otel collector sidecar",
        "lgtm native backends",
        "lgtm observability stack",
        "permissions helper build + sign + load",
        "cross-machine transfer backend",
        "github PR capability",
        "macOS firewall allow list",
        "Homebrew formula pins",
        "reap legacy-named sessions",
        "screen capture availability",
        "accessibility availability",
        "health probe cron job",
        "watchdog probe job",
        "hold watchdog job",
    }
)


def _skip_host_integration(_ctx: converge_host.ConvergeCtx) -> None:
    return


def _hermetic_converge_steps() -> tuple[converge_host.ConvergeStep, ...]:
    # meiva:note
    # Replace only production steps that reach host services with no-ops so the test runs
    # the real CONVERGE_STEPS tuple hermetically without touching host-wide infrastructure.
    return tuple(
        replace(step, apply=_skip_host_integration)
        if step.name in _HOST_INTEGRATION_STEP_NAMES
        else step
        for step in converge_host.CONVERGE_STEPS
    )


def _install_memory_plugin() -> None:
    write_local({"plugins": {"meiva_memory": {"enabled": True}}})


def _make_dirty_memory_repo(pool: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(pool)], check=True)  # noqa: S603
    # `git commit` would otherwise spawn a detached `git maintenance run --auto` that holds
    # `.git/objects/maintenance.lock` while the test walks the tree.
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
        subprocess.run(["git", "-C", str(pool), "config", key, value], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "user.email", "meiva@test.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(pool), "config", "user.name", "Meiva Test"], check=True)  # noqa: S603
    tracked = pool / "tracked.md"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(pool), "add", "tracked.md"], check=True)  # noqa: S603
    subprocess.run(["git", "-C", str(pool), "commit", "-qm", "seed"], check=True)  # noqa: S603
    tracked.write_text("dirty\n", encoding="utf-8")
    (pool / "untracked.md").write_text("untracked\n", encoding="utf-8")


def test_converge_ignores_a_dirty_wrong_branch_memory_pool(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Converge must not invoke Git in runtime memory paths, even when poisoned."""
    _install_memory_plugin()
    command_runtime.machine.set(role="agent-runner", name="test-runner")
    memory_pool = paths.memory_dir()
    _make_dirty_memory_repo(memory_pool, "main")

    converge_host.converge_host(
        tmp_path / "repo",
        frozenset({"agent-runner"}),
        steps=_hermetic_converge_steps(),
        services=frozenset(),
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert (memory_pool / "tracked.md").read_text(encoding="utf-8") == "dirty\n"
    assert (memory_pool / "untracked.md").read_text(encoding="utf-8") == "untracked\n"
    assert not (memory_pool / "MEMORY.md").exists()
    assert not (memory_pool / ".githooks").exists()


def test_memory_init_returns_a_clean_error_for_the_wrong_branch_guard(
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    _install_memory_plugin()
    command_runtime.machine.set(role="agent-runner", name="test-runner")
    _make_dirty_memory_repo(paths.memory_dir(), "main")
    assert memory.cmd_memory_init(machine=command_runtime.machine) == 1

    stderr = capsys.readouterr().err
    assert (
        f"✗ {paths.memory_dir()} is already a git repo but on branch 'main', "
        "expected 'machine-test-runner'."
    ) in stderr
    assert "Manually switch:" in stderr
    assert "Traceback" not in stderr


def test_memory_init_seeds_a_dirty_correct_branch_pool(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    _install_memory_plugin()
    command_runtime.machine.set(role="agent-runner", name="test-runner")
    pool = paths.memory_dir()
    _make_dirty_memory_repo(pool, "machine-test-runner")

    assert memory.cmd_memory_init(machine=command_runtime.machine) == 0

    assert (pool / "MEMORY.md").is_file()
    assert (pool / ".githooks" / "pre-commit").is_file()
    assert (
        subprocess.run(  # noqa: S603
            ["git", "-C", str(pool), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == ".githooks"
    )
    assert "scaffolded: meiva_memory" in capsys.readouterr().out


def test_memory_init_reports_scaffolded_plugins(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    plugin = paths.plugins_dir() / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text("__description__ = 'test scaffold'\n", encoding="utf-8")
    (plugin / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold(*, machine):\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('ready')\n",
        encoding="utf-8",
    )
    _enable_user_plugins({"example": {"enabled": True}})

    assert memory.cmd_memory_init(machine=command_runtime.machine) == 0
    assert (plugin / "scaffolded.marker").read_text(encoding="utf-8") == "ready"
    assert "scaffolded: example" in capsys.readouterr().out


def test_memory_init_parser_binds_the_explicit_handler() -> None:
    from cli import parsers
    from cli.commands.extensions.parsers import mcp

    args = parsers.parse_args(["memory", "init"])
    assert args.func is mcp.h_memory_init


def test_scaffolds_borrow_the_same_invocation_machine(command_runtime: CommandRuntime) -> None:
    command_runtime.machine.set(name="scaffold-owner", role="agent-runner")
    for name in ("first", "second"):
        plugin = paths.plugins_dir() / name
        plugin.mkdir(parents=True)
        (plugin / "plugin.py").write_text("__description__ = 'same owner'\n", encoding="utf-8")
        (plugin / "setup.py").write_text(
            "from pathlib import Path\n"
            "def scaffold(*, machine):\n"
            "    Path(__file__).with_name('owner').write_text(f'{id(machine)}:{machine.name()}')\n",
            encoding="utf-8",
        )
    _enable_user_plugins({"first": {"enabled": True}, "second": {"enabled": True}})

    assert run_plugin_scaffolds(machine=command_runtime.machine).ran == ["first", "second"]
    expected = f"{id(command_runtime.machine)}:scaffold-owner"
    for name in ("first", "second"):
        assert (paths.plugins_dir() / name / "owner").read_text(encoding="utf-8") == expected


def test_legacy_zero_argument_scaffold_fails_fast(command_runtime: CommandRuntime) -> None:
    plugin = paths.plugins_dir() / "legacy"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text("__description__ = 'legacy'\n", encoding="utf-8")
    (plugin / "setup.py").write_text(
        "def scaffold():\n    raise AssertionError('not entered')\n", encoding="utf-8"
    )
    _enable_user_plugins({"legacy": {"enabled": True}})

    with pytest.raises(TypeError, match="machine"):
        run_plugin_scaffolds(machine=command_runtime.machine)
