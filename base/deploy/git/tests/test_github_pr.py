import subprocess

import pytest

import base.deploy.git.github_pr as gp
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("git@github.com:example-org/MeivaMemory.git", "example-org/MeivaMemory"),
        ("https://github.com/example-org/MeivaMemory.git", "example-org/MeivaMemory"),
        ("https://github.com/owner/name", "owner/name"),
    ],
)
def test_slug_parses_selected_github_remote(remote: str, slug: str) -> None:
    assert gp.memory_repo_slug(remote) == slug


def test_slug_raises_on_non_github_remote() -> None:
    with pytest.raises(ValueError, match="not a GitHub remote"):
        gp.memory_repo_slug("git@gitlab.com:owner/name.git")


def _host(commands: FakeCommands, *, gh: str | None = "/usr/bin/gh") -> Host:
    return fake_host(commands, programs={"gh": gh} if gh else {})


def test_auth_and_permission_commands_keep_their_deadlines() -> None:
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply())
    commands.on(["gh", "repo", "view"], Reply(stdout="WRITE\n"))
    host = _host(commands)
    assert gp.github_auth_blocker(host=host) is None
    assert gp.github_permission_blocker(host=host, slug="owner/MeivaMemory") is None
    assert commands.argvs == [
        ("gh", "auth", "status"),
        (
            "gh",
            "repo",
            "view",
            "owner/MeivaMemory",
            "--json",
            "viewerPermission",
            "-q",
            ".viewerPermission",
        ),
    ]
    assert [call.timeout for call in commands.calls] == [20, 20]


def test_blocker_gh_missing() -> None:
    commands = FakeCommands()
    assert gp.github_auth_blocker(host=_host(commands, gh=None)) == "gh CLI not installed"
    assert commands.calls == []


def test_blocker_not_authenticated() -> None:
    commands = FakeCommands()
    commands.on("gh", Reply(returncode=1))
    assert "not authenticated" in str(gp.github_auth_blocker(host=_host(commands)))
    assert commands.argvs == [("gh", "auth", "status")]


@pytest.mark.parametrize("permission", ["WRITE", "MAINTAIN", "ADMIN"])
def test_write_or_better_is_accepted(permission: str) -> None:
    commands = FakeCommands()
    commands.on(["gh", "repo", "view"], Reply(stdout=permission))
    assert gp.github_permission_blocker(host=_host(commands), slug="owner/MeivaMemory") is None


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            Reply(stdout="READ"),
            "gh account lacks write access to owner/MeivaMemory (viewerPermission='READ')",
        ),
        (Reply(returncode=1), "could not read permission on owner/MeivaMemory via gh"),
        (Reply(stdout=""), "could not read permission on owner/MeivaMemory via gh"),
    ],
)
def test_permission_failure_is_a_reason(reply: Reply, reason: str) -> None:
    commands = FakeCommands()
    commands.on(["gh", "repo", "view"], reply)
    assert gp.github_permission_blocker(host=_host(commands), slug="owner/MeivaMemory") == reason


def test_blocker_auth_timeout_is_reason_not_crash() -> None:
    commands = FakeCommands()
    commands.on("gh", subprocess.TimeoutExpired(cmd="gh", timeout=20))
    assert gp.github_auth_blocker(host=_host(commands)) == "gh auth status timed out"


def test_permission_timeout_is_reason_not_crash() -> None:
    commands = FakeCommands()
    commands.on("gh", subprocess.TimeoutExpired(cmd="gh", timeout=20))
    assert (
        gp.github_permission_blocker(host=_host(commands), slug="owner/MeivaMemory")
        == "reading permission on owner/MeivaMemory via gh timed out"
    )
