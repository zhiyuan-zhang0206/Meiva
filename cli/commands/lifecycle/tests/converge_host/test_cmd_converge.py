# meiva:note
# `meiva converge` end to end: the whole step table over a fake OS user's prod install.
#
# Which steps run for a unit, and why the others do not, is `step_selection`'s
# (`test_converge_selection.py`); each step has its own tests. These tests run the command as the
# operator would: the user's home, the shell rc, the unit's home and its checkout are real
# directories under `tmp_path`, the fake user's home being the process `HOME`, so the effects are
# files to read back.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg
import pytest

from base.packages.extensions import install_registry
from base.packages.extensions import registry as reg
from base.paths import meiva_home
from base.telemetry import EventPipeline
from cli.commands.converge.incomplete import read_incomplete
from cli.commands.converge.plans.shell_rc import PATH_BEGIN
from cli.commands.lifecycle.converge_host import cmd_converge
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


# meiva:note
# One `meiva converge` of a prod install: an agent-runner whose home is `<user>/.meiva` and
# whose checkout (`<home>/source`) carries one built-in skill.
@dataclass(frozen=True)
class _Converge:
    user_home: Path
    home: Path
    checkout: Path
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]

    def run(self) -> int:
        return cmd_converge(
            repo=self.checkout,
            only_services=("pty-sessions",),
            disabled_services=(),
            all_services=False,
            database_factory=self.database_factory,
            producer=self.producer,
            command=self.command,
        )


@pytest.fixture
def converge(
    default_home: Path,
    redis_url: str,
    db_url: str,
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> _Converge:
    # meiva:note
    # Requested so the command's configuration names the suite's throwaway Redis and Postgres,
    # which the health preflight and the extension registry read; nothing else is dialled.
    del redis_url, db_url
    home = default_home / ".meiva"
    checkout = home / "source"
    skill = checkout / "meiva_builtins" / "skills" / "converge-demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: converge-demo\ndescription: A skill the converge test lands.\n---\n\nBody.\n"
    )
    session = UserSession(user_home=default_home, login_shell="/bin/zsh", interactive=False)
    command = replace(command_runtime, home=home, session=session)
    command.machine.set(role="agent-runner")
    command.config.set_field("os_jobs_enabled", False)
    return _Converge(default_home, home, checkout, command, operator_database, operator_pipeline)


def test_converge_wires_the_prod_install_and_lays_out_its_home(
    converge: _Converge, capsys: pytest.CaptureFixture[str]
) -> None:
    converge.command.config.set_field("require_github_pr", False)

    assert converge.run() == 0

    home, user_home = converge.home, converge.user_home
    for sub in ("logs", "configs", "secrets"):
        assert (home / sub).is_dir()
    assert (user_home / ".local" / "bin" / "meiva").readlink() == (
        converge.checkout / ".venv" / "bin" / "meiva"
    )
    wired = [
        path for path in user_home.iterdir() if path.is_file() and PATH_BEGIN in path.read_text()
    ]
    assert len(wired) == 1, "one shell rc file carries the PATH block"
    assert (home / "skills" / "converge-demo" / "SKILL.md").is_file()
    out = capsys.readouterr().out
    assert "  ✓ meiva on PATH" in out and "  ✓ $MEIVA_HOME dir skeleton" in out
    assert read_incomplete(home) is None


def test_a_second_converge_changes_nothing_in_the_users_shell(converge: _Converge) -> None:
    converge.command.config.set_field("require_github_pr", False)
    assert converge.run() == 0
    (rc,) = [
        path
        for path in converge.user_home.iterdir()
        if path.is_file() and PATH_BEGIN in path.read_text()
    ]
    before = rc.read_text()

    assert converge.run() == 0

    assert rc.read_text() == before
    assert before.count(PATH_BEGIN) == 1


# meiva:note
# A step that cannot apply stops the converge where it is and leaves the marker `meiva status`
# reports, because `meiva start` goes on to bring the services up past it; only a converge that
# completes clears the marker.
def test_a_failing_step_stops_converge_and_leaves_the_marker_until_one_completes(
    converge: _Converge, capsys: pytest.CaptureFixture[str]
) -> None:
    converge.command.config.set_field("require_github_pr", True)

    with pytest.raises(RuntimeError):
        converge.run()

    captured = capsys.readouterr()
    assert "  ✗ github PR capability" in captured.err
    assert "macOS firewall allow list" not in captured.out, "the steps after it never ran"
    recorded = read_incomplete(converge.home)
    assert recorded is not None and recorded.step == "github PR capability"
    assert (converge.home / "logs").is_dir(), "the steps before it were applied"

    converge.command.config.set_field("require_github_pr", False)
    assert converge.run() == 0
    assert read_incomplete(converge.home) is None


_SKILL = """---
name: {name}
description: a test skill, use when exercising what converge does with extensions
---

# {name}

{body}
"""


def _write_skill(root: Path, name: str, body: str) -> Path:
    tree = root / name
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(_SKILL.format(name=name, body=body), encoding="utf-8")
    return tree


# meiva:note
# The standalone converge runs against a cluster that is already up, so after its steps it
# lands the skills another machine of the cluster installed: the registry row becomes a tree
# under this home's `skills/`.
def test_converge_lands_the_extensions_the_cluster_holds(
    converge: _Converge, capsys: pytest.CaptureFixture[str]
) -> None:
    converge.command.config.set_field("require_github_pr", False)
    source = _write_skill(converge.user_home / "elsewhere", "cluster-skill", "From the cluster.")
    with converge.database_factory().pool() as pool:
        reg.register_tree(
            pool, root=source, name="cluster-skill", kind="skill", source="local:other-box"
        )

    assert converge.run() == 0

    landed = converge.home / "skills" / "cluster-skill" / "SKILL.md"
    assert "From the cluster." in landed.read_text(encoding="utf-8")
    assert "landed: cluster-skill" in capsys.readouterr().out


# meiva:note
# A skill this machine installed before the registry existed is only a line in `installed.json`
# and a tree on disk; converge uploads it under this machine's name so the cluster holds it too,
# and a second converge finds it already claimed.
def test_converge_adopts_what_this_machine_installed_before_the_registry(
    converge: _Converge, db_conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    converge.command.config.set_field("require_github_pr", False)
    converge.command.machine.set(name="converge-box")
    _write_skill(converge.home / "skills", "pre-registry", "Installed long ago.")
    install_registry.register(
        install_registry.InstalledPackage(
            name="pre-registry", type="skill", origin="user", enabled=True, trust="unreviewed"
        ),
        home=meiva_home(),
    )
    assert reg.get(db_conn, "pre-registry") is None

    assert converge.run() == 0

    row = reg.get(db_conn, "pre-registry")
    assert row is not None and row.source == "local:converge-box"
    assert "adopted into the cluster: pre-registry" in capsys.readouterr().out

    assert converge.run() == 0
    assert "adopted into the cluster" not in capsys.readouterr().out
