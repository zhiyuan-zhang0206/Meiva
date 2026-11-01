# meiva:note
# The dependency table, the diagnosis of a reading and the gate's plan, asserted as values.
#
# Nothing here looks at a host: a test states the selection it wants as a `StartScope` and the
# readings the shell would have recorded, and asserts which tools are required, what is wrong
# with them and what the gate decides and prints. `test_host_dependency_gate.py` runs the shell
# against real executables.

from __future__ import annotations

from pathlib import Path

import pytest

from base.host import brew_pin
from cli.commands.data_plane.host_dependencies import table
from cli.commands.data_plane.host_dependencies.diagnosis import (
    Absent,
    DependencyProblem,
    Found,
    NodeVersion,
    NoLocalPlane,
    ProbeFailed,
    Reading,
    Unusable,
    diagnose,
    missing_tools,
)
from cli.commands.data_plane.host_dependencies.gate_plan import (
    InstallGroups,
    Ready,
    Refuse,
    Satisfied,
    plan_gate,
    plan_recheck,
)
from cli.commands.data_plane.host_dependencies.install_plan import (
    exit_line,
    launch_failure_line,
    missing_script_line,
    plan_install,
)
from cli.commands.data_plane.host_dependencies.scope import PlaneFacts, StartScope
from cli.commands.data_plane.host_dependencies.table import Component, HostTool

GATEWAY = frozenset({"gateway"})
RUNNER = frozenset({"agent-runner"})
FULL_ROSTER = frozenset({"gateway", "frontend", "ops", "agent-host"})
FRONTEND_ONLY = frozenset({"frontend"})
HEADLESS = frozenset({"gateway"})
LOCAL_PLANE = PlaneFacts(is_remote=False, pgbouncer_enabled=True)
REPO = Path("/repo")


def _scope(
    *,
    roles: frozenset[str] | None = GATEWAY,
    services: frozenset[str] = FULL_ROSTER,
    data_plane: PlaneFacts | None = LOCAL_PLANE,
    macos: bool = False,
    is_root: bool = False,
    os_jobs: bool = True,
    permissions_helper: bool = False,
    require_github_pr: bool = False,
    memory_keep_local: bool = False,
    prod_install: bool = True,
) -> StartScope:
    return StartScope(
        roles=roles,
        services=services,
        data_plane=data_plane,
        macos=macos,
        is_root=is_root,
        os_jobs=os_jobs,
        permissions_helper=permissions_helper,
        require_github_pr=require_github_pr,
        memory_keep_local=memory_keep_local,
        prod_install=prod_install,
    )


def _names(scope: StartScope) -> set[str]:
    return {tool.name for tool in table.required_tools(scope)}


# meiva:note
# Every required tool read as present, except the ones the test names.
def _present(scope: StartScope, **replaced: Reading) -> dict[str, Reading]:
    readings: dict[str, Reading] = {}
    for tool in table.required_tools(scope):
        readings[tool.name] = (
            NodeVersion("22.14.0")
            if tool.name == "node"
            else Found(f"/usr/bin/{tool.name}", executable=True)
        )
    return {**readings, **replaced}


def _tool(name: str) -> HostTool:
    return next(tool for tool in table.HOST_TOOLS if tool.name == name)


def _problems(scope: StartScope, readings: dict[str, Reading]) -> dict[str, DependencyProblem]:
    return {tool.name: problem for tool, problem in missing_tools(scope, readings)}


def test_gateway_with_the_frontend_requires_the_full_table() -> None:
    assert _names(_scope()) == {
        "initdb",
        "pg_ctl",
        "postgres",
        "pg_isready",
        "redis-server",
        "redis-cli",
        "pgbouncer",
        "node",
        "pnpm",
        "npm",
        "npx",
        "crontab",
        "systemctl",
        "sudo",
        "curl",
        "sh",
        "git",
    }


def test_headless_gateway_needs_no_frontend_tools() -> None:
    assert _names(_scope(services=HEADLESS)).isdisjoint({"node", "pnpm", "npm", "npx", "curl"})


def test_runner_only_host_skips_the_local_data_plane() -> None:
    names = _names(_scope(roles=RUNNER, data_plane=None, services=FRONTEND_ONLY))
    assert names.isdisjoint(
        {"postgres", "pg_ctl", "initdb", "pg_isready", "redis-server", "redis-cli", "pgbouncer"}
    )
    assert {"node", "crontab", "systemctl", "sh"} <= names


def test_remote_plane_skips_the_local_data_plane() -> None:
    remote = PlaneFacts(is_remote=True, pgbouncer_enabled=True)
    assert _names(_scope(data_plane=remote)).isdisjoint({"redis-server", "pgbouncer", "postgres"})


def test_os_jobs_off_skips_the_scheduler_rows() -> None:
    assert _names(_scope(os_jobs=False)).isdisjoint({"crontab", "systemctl", "sudo", "launchctl"})


def test_pooling_off_drops_pgbouncer() -> None:
    plane = PlaneFacts(is_remote=False, pgbouncer_enabled=False)
    assert "pgbouncer" not in _names(_scope(data_plane=plane))


def test_macos_uses_launchctl_and_the_helper_tools() -> None:
    names = _names(_scope(macos=True, permissions_helper=True))
    assert {"launchctl", "swiftc", "codesign", "security", "openssl"} <= names
    assert names.isdisjoint({"crontab", "systemctl", "sudo"})


def test_root_does_not_require_sudo() -> None:
    assert "sudo" not in _names(_scope(is_root=True))


def test_git_is_required_on_every_start() -> None:
    selections: tuple[tuple[frozenset[str] | None, frozenset[str]], ...] = (
        (GATEWAY, HEADLESS),
        (RUNNER, frozenset()),
        (None, frozenset()),
    )
    for roles, services in selections:
        scope = _scope(roles=roles, services=services, data_plane=None)
        assert "git" in _names(scope)


def test_a_split_runner_consolidating_by_pr_needs_the_github_cli() -> None:
    scope = _scope(roles=RUNNER, data_plane=None, require_github_pr=True)
    assert "gh" in _names(scope)


def test_a_gateway_never_needs_the_github_cli() -> None:
    assert "gh" not in _names(_scope(require_github_pr=True))


def test_a_local_memory_pool_or_an_explicit_opt_out_drops_the_github_cli() -> None:
    local = _scope(roles=RUNNER, data_plane=None, require_github_pr=True, memory_keep_local=True)
    opted_out = _scope(roles=RUNNER, data_plane=None, require_github_pr=False)
    assert "gh" not in _names(local)
    assert "gh" not in _names(opted_out)


def test_a_dev_worktree_start_does_not_require_the_boot_unit() -> None:
    names = _names(_scope(prod_install=False))
    assert "systemctl" not in names
    assert "sudo" not in names
    assert {"crontab", "git"} <= names


def test_optional_tools_are_never_required() -> None:
    assert {tool.name for tool in table.HOST_TOOLS if tool.optional} == {"brew", "lsof", "ps"}
    assert all(not tool.optional for tool in table.required_tools(_scope()))


def test_every_group_names_its_provisioning_script_and_label() -> None:
    groups = {tool.component for tool in table.HOST_TOOLS if tool.component is not None}
    assert groups == set(Component)
    for group in Component:
        assert table.provision_script(group).endswith(".sh")
        assert table.provision_label(group)


def test_a_group_the_table_does_not_name_is_refused_where_it_is_parsed() -> None:
    with pytest.raises(ValueError, match="'elsewhere' is not a valid Component"):
        Component("elsewhere")


def test_all_present_is_no_problem() -> None:
    scope = _scope()
    assert missing_tools(scope, _present(scope)) == ()


def test_the_postgres_reading_names_only_the_missing_tool() -> None:
    scope = _scope()
    problems = _problems(scope, _present(scope, pg_isready=Absent()))
    assert set(problems) == {"pg_isready"}
    assert "vendored" in problems["pg_isready"].detail
    assert problems["pg_isready"].installable


def test_a_postgres_tool_that_cannot_run_names_its_path() -> None:
    scope = _scope()
    problems = _problems(scope, _present(scope, initdb=Found("/opt/pg/initdb", executable=False)))
    assert problems == {"initdb": DependencyProblem("/opt/pg/initdb is not executable")}


def test_the_pgbouncer_reading_reports_the_resolved_path() -> None:
    scope = _scope()
    problems = _problems(scope, _present(scope, pgbouncer=Found("/missing/pgbouncer", False)))
    assert set(problems) == {"pgbouncer"}
    assert "/missing/pgbouncer" in problems["pgbouncer"].detail


def test_a_bare_pgbouncer_name_means_it_is_not_installed() -> None:
    problem = diagnose(_tool("pgbouncer"), Found("pgbouncer", executable=False))
    assert problem == DependencyProblem("not on PATH (pgbouncer is not installed)")


def test_an_unusable_redis_override_is_not_installable() -> None:
    scope = _scope(services=HEADLESS)
    reason = "MEIVA_REDIS_BIN_DIR requires an executable file"
    problems = _problems(scope, _present(scope, **{"redis-server": Unusable(reason)}))
    assert set(problems) == {"redis-server"}
    assert "MEIVA_REDIS_BIN_DIR" in problems["redis-server"].detail
    assert not problems["redis-server"].installable


def test_a_redis_binary_that_cannot_run_is_installable() -> None:
    problem = diagnose(_tool("redis-cli"), Found("/missing/redis-cli", executable=False))
    assert problem == DependencyProblem("/missing/redis-cli is not executable")


def test_a_selection_without_a_data_plane_has_no_redis_to_resolve() -> None:
    problem = diagnose(_tool("redis-server"), NoLocalPlane())
    assert problem == DependencyProblem("no local data plane in this selection", installable=False)


def test_node_at_the_floor_or_above_is_usable() -> None:
    for version in ("22.13.0", "22.14.0", "25.6.1"):
        assert diagnose(_tool("node"), NodeVersion(version)) is None


def test_node_older_than_the_floor_names_its_version() -> None:
    problem = diagnose(_tool("node"), NodeVersion("20.9.0"))
    assert problem == DependencyProblem("20.9.0 is older than 22.13")


def test_a_node_version_that_does_not_parse_is_older_than_the_floor() -> None:
    assert diagnose(_tool("node"), NodeVersion("")) == DependencyProblem(
        "version unknown is older than 22.13"
    )
    assert diagnose(_tool("node"), NodeVersion("v22")) == DependencyProblem(
        "v22 is older than 22.13"
    )


def test_a_node_whose_version_cannot_be_read_is_a_problem() -> None:
    problem = diagnose(_tool("node"), ProbeFailed("timed out"))
    assert problem == DependencyProblem("version probe failed: timed out")


def test_a_missing_node_toolchain_binary_names_itself() -> None:
    assert diagnose(_tool("pnpm"), Absent()) == DependencyProblem("pnpm is not on PATH")
    assert diagnose(_tool("npm"), Absent()) == DependencyProblem("npm is not on PATH")
    assert diagnose(_tool("npx"), Found("/usr/bin/npx", executable=True)) is None


def test_a_program_missing_from_path_says_so() -> None:
    assert diagnose(_tool("crontab"), Absent()) == DependencyProblem("not on PATH")


def test_the_shell_and_systemd_have_their_own_wording() -> None:
    assert diagnose(_tool("sh"), Absent()) == DependencyProblem("/bin/sh is missing")
    systemd = diagnose(_tool("systemctl"), Absent())
    assert systemd is not None and "systemd is not this host's service manager" in systemd.detail


def test_a_reading_of_the_wrong_kind_is_a_defect_not_a_host_condition() -> None:
    with pytest.raises(ValueError, match="not a PostgreSQL tool reading"):
        diagnose(_tool("initdb"), NodeVersion("22.0.0"))
    with pytest.raises(ValueError, match="not a presence reading"):
        diagnose(_tool("git"), Unusable("x"))


def test_a_required_tool_without_a_reading_raises() -> None:
    scope = _scope()
    readings = _present(scope)
    del readings["git"]
    with pytest.raises(KeyError):
        missing_tools(scope, readings)


def _missing(
    scope: StartScope, **replaced: Reading
) -> tuple[tuple[HostTool, DependencyProblem], ...]:
    return missing_tools(scope, _present(scope, **replaced))


def test_all_present_is_silent() -> None:
    scope = _scope()
    assert plan_gate(_missing(scope), repo=REPO, macos=False) == Satisfied()


def test_a_missing_installable_tool_installs_its_group_and_lists_it() -> None:
    scope = _scope()
    plan = plan_gate(_missing(scope, crontab=Absent()), repo=REPO, macos=False)
    assert plan == InstallGroups(
        report=("", "→ host dependencies", "  · crontab: not on PATH"),
        groups=(Component.HOST_UTILS,),
    )


def test_each_missing_group_installs_once_in_table_order() -> None:
    scope = _scope()
    plan = plan_gate(
        _missing(scope, pg_isready=Absent(), initdb=Absent(), curl=Absent(), git=Absent()),
        repo=REPO,
        macos=False,
    )
    assert isinstance(plan, InstallGroups)
    assert plan.groups == (Component.DATA_PLANE, Component.HOST_UTILS, Component.CLI_TOOLS)


def test_a_host_owned_tool_refuses_before_any_install() -> None:
    scope = _scope()
    plan = plan_gate(_missing(scope, crontab=Absent(), systemctl=Absent()), repo=REPO, macos=False)
    assert isinstance(plan, Refuse)
    assert plan.report[:2] == ("", "→ host dependencies")
    assert "    ✗ crontab: not on PATH" in plan.refusal
    assert any(line.startswith("    ✗ systemctl:") for line in plan.refusal)
    assert (
        "    install systemd and boot with it, or run `MEIVA_OS_JOBS_ENABLED=0 meiva start` to start without OS jobs"
        in plan.refusal
    )


def test_a_missing_tool_that_stays_missing_names_the_tool_and_the_command() -> None:
    scope = _scope()
    remaining = _missing(scope, crontab=Absent())
    refusal = plan_recheck(remaining, repo=REPO, macos=False)
    assert isinstance(refusal, Refuse)
    assert refusal.report == ()
    assert (
        refusal.refusal[1] == "✗ host dependency preflight failed: `meiva start` launched nothing."
    )
    assert "    ✗ crontab: not on PATH" in refusal.refusal
    assert f"    Debian/Ubuntu: sudo bash {REPO}/scripts/provision/host_utils.sh" in refusal.refusal
    assert "  Fix the host, then retry `meiva start`:" in refusal.refusal


def test_a_host_owned_tool_refuses_with_the_escape_command() -> None:
    scope = _scope()
    plan = plan_gate(_missing(scope, systemctl=Absent()), repo=REPO, macos=False)
    assert isinstance(plan, Refuse)
    assert any("MEIVA_OS_JOBS_ENABLED=0" in line for line in plan.refusal)


def test_node_older_than_the_floor_refuses() -> None:
    scope = _scope(roles=RUNNER, data_plane=None, services=FRONTEND_ONLY)
    plan = plan_gate(_missing(scope, node=NodeVersion("18.19.1")), repo=REPO, macos=False)
    assert isinstance(plan, InstallGroups), "an old Node is fixed by the group's install"
    final = plan_recheck(_missing(scope, node=NodeVersion("18.19.1")), repo=REPO, macos=False)
    assert isinstance(final, Refuse)
    assert "    ✗ node: 18.19.1 is older than 22.13" in final.refusal


def test_an_unusable_redis_override_refuses_without_installing() -> None:
    scope = _scope(services=HEADLESS)
    missing = _missing(
        scope, **{"redis-server": Unusable("MEIVA_REDIS_BIN_DIR requires an executable file")}
    )
    plan = plan_gate(missing, repo=REPO, macos=False)
    assert isinstance(plan, Refuse)
    assert any("MEIVA_REDIS_BIN_DIR" in line for line in plan.refusal)


def test_a_mac_tool_refuses_with_xcode_select() -> None:
    scope = _scope(
        roles=RUNNER,
        data_plane=None,
        services=HEADLESS,
        macos=True,
        os_jobs=False,
        permissions_helper=True,
    )
    plan = plan_gate(_missing(scope, codesign=Absent()), repo=REPO, macos=True)
    assert isinstance(plan, Refuse)
    assert "    ✗ codesign: not on PATH" in plan.refusal
    assert "    install the Apple Command Line Tools: `xcode-select --install`" in plan.refusal


def test_macos_remediation_names_homebrew_instead_of_the_provision_scripts() -> None:
    scope = _scope(macos=True, os_jobs=False)
    plan = plan_recheck(
        _missing(scope, pg_isready=Absent(), pnpm=Absent(), git=Absent()), repo=REPO, macos=True
    )
    assert isinstance(plan, Refuse)
    assert "    macOS needs Homebrew first: https://brew.sh" in plan.refusal
    assert "    brew install postgresql@17 redis@8.2 pgbouncer pgvector" in plan.refusal
    assert f"    brew install node && npm install -g pnpm@{brew_pin.PNPM_VERSION}" in plan.refusal
    assert "    brew install git gh" in plan.refusal
    assert not any("Debian/Ubuntu" in line for line in plan.refusal)


def test_after_the_install_a_complete_host_is_ready() -> None:
    assert plan_recheck((), repo=REPO, macos=False) == Ready("  ✓ host dependencies ready")


def test_an_install_runs_the_groups_script_through_bash() -> None:
    step = plan_install(Component.HOST_UTILS, repo=REPO, macos=False)
    assert step.script == REPO / "scripts/provision/host_utils.sh"
    assert step.argv == ("bash", str(step.script))
    assert step.announcement == (
        "  · installing cron + curl with apt (this can take several minutes)"
    )


def test_an_install_on_macos_names_homebrew() -> None:
    step = plan_install(Component.NODE, repo=REPO, macos=True)
    assert (
        step.announcement == "  · installing Node.js with Homebrew (this can take several minutes)"
    )


def test_the_install_failure_lines_name_the_script() -> None:
    script = REPO / "scripts/provision/node.sh"
    assert missing_script_line(script) == f"  ✗ provision script missing: {script}"
    assert launch_failure_line(script, "boom") == "  ✗ node.sh failed to run: boom"
    assert exit_line(script, 3) == "  ! node.sh exited 3; re-checking what landed"
