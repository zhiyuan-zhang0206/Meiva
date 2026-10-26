# meiva:note
# The GitHub PR capability check: which hosts it applies to, and how it fails.
#
# The `gh` CLI is the one program the check cannot run for real, so the hosts here answer its
# commands from a script (`FakeCommands` refuses any command the script does not name, which also
# proves the single-box and opt-out cases never reach for `gh`).

from __future__ import annotations

from dataclasses import replace

import pytest

import cli.commands.lifecycle.converge_host as cv
from base.deploy.git.memory_repo import MemoryRemoteMissing
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from cli.commands.converge.plans.split_host_checks import github_pr_failure, requires_github_pr
from cli.commands.converge.spec import ConvergeCtx

# meiva:note
# --- which hosts must be able to open PRs (pure) ------------------------------------


@pytest.mark.parametrize(
    ("roles", "keep_local", "require_pr", "required"),
    [
        (frozenset({"agent-runner"}), False, True, True),
        (None, False, True, True),
        # meiva:note
        # consolidates locally
        (frozenset({"gateway", "agent-runner"}), False, True, False),
        # meiva:note
        # a local-only memory pool
        (frozenset({"agent-runner"}), True, True, False),
        # meiva:note
        # a runner that opted out
        (frozenset({"agent-runner"}), False, False, False),
    ],
)
def test_only_a_split_runner_that_consolidates_via_prs_must_open_them(
    roles: frozenset[str] | None, keep_local: bool, require_pr: bool, required: bool
) -> None:
    assert requires_github_pr(roles=roles, keep_local=keep_local, require_pr=require_pr) is required


def test_the_failure_names_the_reason_and_the_repair() -> None:
    message = github_pr_failure("gh CLI not installed")

    assert message.startswith("this agent-runner cannot open+merge GitHub PRs")
    assert "gh CLI not installed" in message
    assert "gh auth login" in message


# meiva:note
# --- the step against a host --------------------------------------------------------


def test_raises_when_gh_is_not_authenticated(ctx: ConvergeCtx) -> None:
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply(returncode=1))
    runner = replace(ctx, host=fake_host(commands, programs={"gh": "/usr/bin/gh"}))

    with pytest.raises(RuntimeError, match="not authenticated"):
        cv.ensure_github_pr(runner)


def test_raises_when_gh_is_not_installed(ctx: ConvergeCtx) -> None:
    with pytest.raises(RuntimeError, match="gh CLI not installed"):
        cv.ensure_github_pr(ctx)


def test_skips_on_single_box(ctx: ConvergeCtx) -> None:
    single_box = replace(ctx, roles=frozenset({"gateway", "agent-runner"}))

    # meiva:note
    # no gh on this host, and no failure
    cv.ensure_github_pr(single_box)


def test_skips_when_opt_out(ctx: ConvergeCtx) -> None:
    ctx.config.set_field("require_github_pr", False)

    cv.ensure_github_pr(ctx)


def test_skips_when_the_memory_pool_stays_local(ctx: ConvergeCtx) -> None:
    ctx.config.set_field("memory_keep_local", True)

    cv.ensure_github_pr(ctx)


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "github PR capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True


@pytest.mark.parametrize("authenticated", [False, True])
def test_the_remote_is_read_only_after_authentication(
    ctx: ConvergeCtx,
    authenticated: bool,
) -> None:
    ctx.config.set_field("memory_remote", "")
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply(returncode=0 if authenticated else 1))
    runner = replace(ctx, host=fake_host(commands, programs={"gh": "/usr/bin/gh"}))
    # meiva:note
    # A missing remote would fail immediately if evaluated before auth.
    if authenticated:
        with pytest.raises(MemoryRemoteMissing):
            cv.ensure_github_pr(runner)
    else:
        with pytest.raises(RuntimeError, match="not authenticated"):
            cv.ensure_github_pr(runner)
    assert commands.argvs == [("gh", "auth", "status")]


@pytest.mark.parametrize("permission", ["WRITE", "READ"])
def test_the_permission_probe_asks_about_the_converge_owners_remote(
    ctx: ConvergeCtx,
    permission: str,
) -> None:
    ctx.config.set_field("memory_remote", "git@github.com:owner/pool.git")
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply())
    commands.on(["gh", "repo", "view", "owner/pool"], Reply(stdout=permission))
    runner = replace(ctx, host=fake_host(commands, programs={"gh": "/usr/bin/gh"}))
    if permission == "WRITE":
        cv.ensure_github_pr(runner)
    else:
        with pytest.raises(RuntimeError, match="lacks write access to owner/pool"):
            cv.ensure_github_pr(runner)
    assert commands.argvs[1][3] == "owner/pool"


def test_missing_gh_does_not_require_the_remote(ctx: ConvergeCtx) -> None:
    ctx.config.set_field("memory_remote", "")
    with pytest.raises(RuntimeError, match="gh CLI not installed"):
        cv.ensure_github_pr(ctx)
