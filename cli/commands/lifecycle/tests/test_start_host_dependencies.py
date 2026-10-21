# meiva:note
# The host-dependency gate is the first cold-start step, and the owner doc carries
# the table the code renders.

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64
from cli.commands.data_plane import host_dependencies
from cli.commands.lifecycle.start import cold_start_steps

RUNNER = frozenset({"agent-runner"})
REPO_ROOT = Path(__file__).resolve().parents[4]
CHILD = Path(__file__).with_name("_host_dependencies_path_child.py")


def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def test_host_dependency_step_runs_first() -> None:
    steps = cold_start_steps()
    assert steps[0].name == "host dependencies"
    assert steps[0].apply is host_dependencies.ensure_host_dependencies_step


def _owner_doc() -> Path:
    return (
        Path(host_dependencies.__file__).resolve().parents[1]
        / "lifecycle"
        / "docs"
        / "start-host-dependencies.meiva.okf.md"
    )


def _bullet_names(text: str, label: str) -> set[str]:
    line = next(row for row in text.splitlines() if row.startswith(f"- **{label}**"))
    return set(re.findall(r"`([^`]+)`", line))


def test_the_owner_doc_carries_the_dependency_table() -> None:
    assert host_dependencies.render_table() in _owner_doc().read_text(encoding="utf-8")


# meiva:note
# The prose above the table names the provisionable groups and the optional rows.
# Comparing the table alone let those bullets drift: `git` and `gh` stayed listed
# as optional after they became required rows.
def test_the_owner_doc_names_the_same_groups_and_optional_rows() -> None:
    text = _owner_doc().read_text(encoding="utf-8")
    groups = {tool.component for tool in host_dependencies.HOST_TOOLS if tool.component is not None}
    assert _bullet_names(text, "A provisionable group") == groups
    optional = {tool.name for tool in host_dependencies.HOST_TOOLS if tool.optional}
    assert _bullet_names(text, "An optional tool") == optional


# meiva:note
# A gate refusal over a host this process chooses, as stderr text. The child's
# world holds no executable at all: its `Host` reports nothing on PATH and the
# process PATH is empty too, so a probe that reached for the environment instead of
# the host could not make the outcome depend on the machine running the test.
def _refusal(platform: str, tmp_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(CHILD), str(tmp_path), platform],
        cwd=REPO_ROOT,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    return result.stderr


# meiva:note
# The criterion the task names: remove a required tool from PATH and start must
# refuse, naming the tool and its repair. The child runs the real gate on a Linux
# `Host` whose PATH holds nothing — exactly what a host without crontab looks like
# to it — so the assertion is the same on macOS, where reading the process platform
# would select the launchctl rows instead. That the refusal happens before any
# service launches is structural (the gate is `cold_start_steps()[0]`) and is
# proved end to end by `scripts/verify/host_dependency_refusal.py`.
def test_a_tool_removed_from_path_is_refused_with_its_repair(tmp_path: Path) -> None:
    stderr = _refusal("linux", tmp_path)
    assert "✗ crontab" in stderr
    assert str(tmp_path / "scripts/provision/host_utils.sh") in stderr


# meiva:note
# The macOS half of the same gate: which rows exist and which repair is printed come
# from the injected `Host.platform`, so a Linux CI lane proves the macOS table and a
# macOS developer machine proves the Linux one.
def test_a_macos_host_is_refused_over_launchctl_with_its_repair(tmp_path: Path) -> None:
    stderr = _refusal("darwin", tmp_path)
    assert "✗ launchctl" in stderr
    assert "✗ crontab" not in stderr
    assert "launchd ships with macOS" in stderr


# meiva:note
# A macOS host takes the launchctl rows even while the test process runs on Linux:
# the platform comes from `Host`, never from `sys.platform`.
def test_the_platform_is_read_from_the_host_not_the_process() -> None:
    host = Host(
        run=_run,
        which=lambda name: f"/usr/bin/{name}",
        platform=DARWIN_ARM64,
        uid=1000,
    )
    scope = host_dependencies.StartScope(
        roles=RUNNER,
        services=frozenset({"gateway"}),
        data_plane=None,
        host=host,
        os_jobs=True,
        permissions_helper=False,
        require_github_pr=False,
        memory_keep_local=False,
        prod_install=True,
    )
    names = {tool.name for tool in host_dependencies.required_tools(scope)}
    assert "launchctl" in names
    assert "crontab" not in names
