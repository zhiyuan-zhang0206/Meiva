# meiva:note
# Base freshness reads main from GitHub, never from a local remote: the authenticated API read first,
# then the public ls-remote fallback, so an origin that is a stale managed mirror cannot answer.
from __future__ import annotations

import subprocess

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from scripts.ci.pull_requests import trunk_api

REPO = "zhiyuan-zhang0206/Meiva"
BASE_SHA = "a" * 40
MAIN_SHA = "b" * 40
GITHUB_API_ARGV = ("gh", "api", f"repos/{REPO}/git/ref/heads/main")
LS_REMOTE_ARGV = ("git", "ls-remote", f"https://github.com/{REPO}.git", "refs/heads/main")


def _host(
    *, github_api: Reply | BaseException, ls_remote: Reply | BaseException
) -> tuple[Host, FakeCommands]:
    runner = FakeCommands()
    runner.on(["gh", "pr", "view"], Reply(stdout=f"{BASE_SHA}\n"))
    runner.on(GITHUB_API_ARGV, github_api)
    runner.on(["git", "ls-remote"], ls_remote)
    return fake_host(runner), runner


def test_main_sha_comes_from_the_github_api() -> None:
    host, runner = _host(
        github_api=Reply(stdout=f"{MAIN_SHA}\n"),
        ls_remote=Reply(stdout=f"{MAIN_SHA}\trefs/heads/main\n"),
    )

    assert trunk_api.base_freshness("42", REPO, host=host) == ((BASE_SHA, MAIN_SHA), False)
    assert runner.argvs == [
        ("gh", "pr", "view", "42", "--repo", REPO, "--json", "baseRefOid", "--jq", ".baseRefOid"),
        (*GITHUB_API_ARGV, "--jq", ".object.sha"),
    ]


@pytest.mark.parametrize(
    "github_api",
    [
        Reply(returncode=1, stderr="gh: not authenticated"),
        Reply(stdout="not-a-sha\n"),
        subprocess.TimeoutExpired("gh", 30),
        FileNotFoundError("gh"),
    ],
    ids=["exit-code", "non-sha-output", "timeout", "missing-binary"],
)
def test_unreadable_github_api_falls_back_to_the_public_ls_remote(
    github_api: Reply | BaseException,
) -> None:
    host, runner = _host(
        github_api=github_api,
        ls_remote=Reply(stdout=f"{MAIN_SHA}\trefs/heads/main\n"),
    )

    assert trunk_api.base_freshness("42", REPO, host=host) == ((BASE_SHA, MAIN_SHA), False)
    # meiva:note
    # The fallback names the public GitHub URL rather than a remote of whichever checkout is nearby,
    # and runs with the non-interactive git environment, so a credential prompt cannot stall it.
    assert runner.argvs[-1] == LS_REMOTE_ARGV
    assert runner.calls[-1].kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(
    "ls_remote",
    [
        Reply(returncode=128, stderr="could not read from remote"),
        Reply(stdout="not-a-sha\n"),
        subprocess.TimeoutExpired("git", 30),
        FileNotFoundError("git"),
    ],
    ids=["exit-code", "non-sha-output", "timeout", "missing-binary"],
)
def test_both_sources_failing_reads_as_unknown(ls_remote: Reply | BaseException) -> None:
    host, _ = _host(github_api=Reply(returncode=1, stderr="gh: no token"), ls_remote=ls_remote)

    assert trunk_api.base_freshness("42", REPO, host=host) == (None, True)


def test_base_at_current_main_is_fresh() -> None:
    host, _ = _host(github_api=Reply(stdout=f"{BASE_SHA}\n"), ls_remote=Reply(stdout=""))

    assert trunk_api.base_freshness("42", REPO, host=host) == (None, False)


# meiva:note
# Only the expected read failures degrade to unknown; an unexpected error from the command runner is
# a defect and must surface rather than pass for missing evidence.
def test_unexpected_read_failure_still_raises() -> None:
    host, _ = _host(github_api=RuntimeError("invalid command implementation"), ls_remote=Reply(""))

    with pytest.raises(RuntimeError):
        trunk_api.base_freshness("42", REPO, host=host)
