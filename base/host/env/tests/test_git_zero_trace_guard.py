from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from base.host.env.harness import git_zero_trace_guard as guard
from base.host.env.harness_binaries import (
    GIT_SHIM_DIR,
    meiva_python_env,
    with_git_shim,
    without_git_shim,
)

SELF_ID = 8720
HANDLE_ENV = guard.HANDLE_ENV
PATTERN = "example-corp/"


@dataclass
class World:
    root: Path
    work: Path
    trace_free: Path
    elsewhere: Path
    home: Path

    def env(self, extra: Mapping[str, str] | None = None, *, shim: bool = True) -> dict[str, str]:
        return {
            "HOME": str(self.root),
            "MEIVA_HOME": str(self.home),
            "PATH": with_git_shim(os.environ["PATH"]) if shim else os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            **meiva_python_env(sys.executable),
            **(extra or {}),
        }

    def git(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return self.git_from(self.work, *args, **env)

    def git_from(self, cwd: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=cwd,
            env=self.env(env),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def plain(self, *args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=self.work,
            env=self.env(shim=False),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, message: str, name: str = "f") -> None:
        (self.work / name).write_text(message)
        self.plain("add", "-A")
        self.plain("commit", "-q", "-m", message)

    def remote_branches(self, bare: Path) -> set[str]:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return set(out.split())


@pytest.fixture
def world(tmp_path: Path) -> World:
    trace_free = tmp_path / "example-corp" / "app.git"
    elsewhere = tmp_path / "elsewhere" / "app.git"
    work = tmp_path / "work"
    for bare in (trace_free, elsewhere):
        bare.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)  # noqa: S603
    work.mkdir()
    built = World(tmp_path, work, trace_free, elsewhere, tmp_path / "meiva-home")
    built.plain("init", "-q", "-b", "main")
    built.plain("config", "user.name", "Tester")
    built.plain("config", "user.email", "tester@example.test")
    built.plain("remote", "add", "origin", str(trace_free))
    built.plain("remote", "add", "other", str(elsewhere))
    built.commit("initial")
    return built


def _blocked(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == guard.BLOCKED_EXIT_CODE and "zero-trace guard" in result.stderr


def _machine_git() -> str:
    found = shutil.which("git", path=without_git_shim(os.environ["PATH"]))
    assert found is not None, "no git outside the shim on PATH"
    return found


def test_the_shim_is_first_on_path_and_is_not_entered_twice() -> None:
    path = with_git_shim("/usr/bin")
    assert path.split(os.pathsep)[0] == str(GIT_SHIM_DIR.resolve())
    assert with_git_shim(path) == path
    assert os.access(GIT_SHIM_DIR / "git", os.X_OK)


def test_the_machines_own_git_is_found_past_every_copy_of_the_shim(tmp_path: Path) -> None:
    installed = tmp_path / "installed" / "harness" / "bin"
    installed.mkdir(parents=True)
    (installed.parent / "git_zero_trace_guard.py").write_text("")
    plain = tmp_path / "plain"
    plain.mkdir()
    path = os.pathsep.join([str(installed), str(GIT_SHIM_DIR.resolve()), str(plain)])
    assert without_git_shim(path) == str(plain)
    assert without_git_shim(None) == ""
    assert Path(_machine_git()).name == "git"
    assert Path(_machine_git()).parent != GIT_SHIM_DIR.resolve()


def test_a_minimal_path_needs_the_machines_git_and_not_a_link_to_the_shim(
    world: World, tmp_path: Path
) -> None:
    shim_only = tmp_path / "shim-only"
    shim_only.mkdir()
    (shim_only / "git").symlink_to(GIT_SHIM_DIR.resolve() / "git")
    machine_only = tmp_path / "machine-only"
    machine_only.mkdir()
    (machine_only / "git").symlink_to(_machine_git())

    def version(minimal: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [str(minimal / "git"), "--version"],
            env={**world.env(), "PATH": str(minimal)},
            capture_output=True,
            text=True,
            check=False,
        )

    assert version(machine_only).stdout.startswith("git version")
    refused = version(shim_only)
    assert refused.returncode == 127
    assert "found no other git on PATH" in refused.stderr


def test_nothing_is_configured_so_the_shim_runs_git_untouched(world: World) -> None:
    world.plain("checkout", "-q", "-b", f"agent-{SELF_ID}-x")
    result = world.git("push", "origin", "HEAD", MEIVA_AGENT_ID=str(SELF_ID))
    assert result.returncode == 0, result.stderr
    assert f"agent-{SELF_ID}-x" in world.remote_branches(world.trace_free)
    version = world.git("--version")
    assert version.stdout.startswith("git version")


def test_a_remote_outside_the_pattern_is_not_checked(world: World) -> None:
    world.plain("checkout", "-q", "-b", f"meiva-{SELF_ID}-fix")
    world.commit("work\n\nCo-authored-by: Meiva #8720")
    result = world.git(
        "push", "other", "HEAD", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN, MEIVA_AGENT_ID=str(SELF_ID)
    )
    assert result.returncode == 0, result.stderr
    assert f"meiva-{SELF_ID}-fix" in world.remote_branches(world.elsewhere)


def test_a_clean_branch_with_clean_commits_is_pushed_to_a_guarded_remote(world: World) -> None:
    world.plain("checkout", "-q", "-b", "fix-4367")
    world.commit("fix the parser\n\nPlain body without any trailer.")
    result = world.git(
        "push",
        "-u",
        "origin",
        "fix-4367",
        MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN,
        MEIVA_AGENT_ID=str(SELF_ID),
    )
    assert result.returncode == 0, result.stderr
    assert "fix-4367" in world.remote_branches(world.trace_free)


def test_global_options_before_push_are_skipped_and_the_push_is_still_guarded(world: World) -> None:
    world.plain("checkout", "-q", "-b", f"agent-{SELF_ID}-fix")
    elsewhere_cwd = world.root
    result = world.git_from(
        elsewhere_cwd,
        "-C",
        str(world.work),
        "-c",
        "user.name=Somebody",
        "--no-pager",
        "push",
        "origin",
        "HEAD",
        MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN,
        MEIVA_AGENT_ID=str(SELF_ID),
    )
    assert _blocked(result), result.stderr
    assert f"agent-{SELF_ID}-fix" in result.stderr
    assert "git branch -m" in result.stderr
    assert world.remote_branches(world.trace_free) == set()
    long_form = world.git_from(
        elsewhere_cwd,
        f"--git-dir={world.work / '.git'}",
        "--work-tree",
        str(world.work),
        "-c",
        "a.b=c",
        "push",
        "origin",
        "HEAD",
        MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN,
        MEIVA_AGENT_ID=str(SELF_ID),
    )
    assert _blocked(long_form), long_form.stderr


def test_a_commit_message_that_mentions_push_is_not_a_push(world: World) -> None:
    result = world.git(
        "commit", "--allow-empty", "-m", "push something", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN
    )
    assert result.returncode == 0, result.stderr
    assert world.plain("log", "-1", "--format=%s") == "push something"
    pushed = world.git(
        "-c",
        "alias.x=status",
        "log",
        "-1",
        "--format=%s",
        "--grep=push",
        MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN,
    )
    assert pushed.returncode == 0


def test_the_agent_id_comes_from_the_session_registration_by_handle(world: World) -> None:
    handle = "session-handle-0123456789"
    registrations = world.home / "harness" / "registrations"
    registrations.mkdir(parents=True)
    document = {"agent_id": SELF_ID, "handle_sha256": hashlib.sha256(handle.encode()).hexdigest()}
    (registrations / f"{SELF_ID}.json").write_text(json.dumps(document))
    env = {"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN, HANDLE_ENV: handle}

    world.plain("checkout", "-q", "-b", f"fix-{SELF_ID}")
    blocked = world.git("push", "origin", "HEAD", **env)
    assert _blocked(blocked), blocked.stderr
    assert f"agent's id {SELF_ID}" in blocked.stderr

    world.plain("checkout", "-q", "-b", "fix-4367")
    allowed = world.git("push", "origin", "HEAD", **env)
    assert allowed.returncode == 0, allowed.stderr

    unknown_session = {**env, HANDLE_ENV: "a-handle-no-registration-holds"}
    unresolved = world.git("push", "origin", f"HEAD:fix-{SELF_ID}-b", **unknown_session)
    assert unresolved.returncode == 0, "without an id only the platform words are judged"
    assert f"fix-{SELF_ID}-b" in world.remote_branches(world.trace_free)


@pytest.mark.parametrize("word", ["meiva", "Meizel", "antigravity"])
def test_a_platform_word_in_the_remote_branch_name_is_blocked(world: World, word: str) -> None:
    world.plain("checkout", "-q", "-b", f"feat/{word}-thing")
    result = world.git("push", "origin", "HEAD", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert _blocked(result), result.stderr
    assert world.remote_branches(world.trace_free) == set()


def test_the_remote_name_of_a_refspec_is_what_is_judged(world: World) -> None:
    world.plain("checkout", "-q", "-b", "meiva-local-name")
    env = {"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN}
    clean = world.git("push", "origin", "HEAD:clean-name", **env)
    assert clean.returncode == 0, clean.stderr
    dirty = world.git("push", "origin", "main:refs/heads/meizel-copy", **env)
    assert _blocked(dirty), dirty.stderr
    assert world.remote_branches(world.trace_free) == {"clean-name"}


@pytest.mark.parametrize(
    "message",
    [
        "work\n\nCo-authored-by: Meiva #8720",
        "work\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
        "work\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)",
        "work\n\nsee /home/u/.meiva/workspaces/1/notes.md",
    ],
)
def test_an_ai_trailer_or_platform_path_in_a_pending_commit_is_blocked(
    world: World, message: str
) -> None:
    world.plain("checkout", "-q", "-b", "clean-name")
    world.commit(message)
    result = world.git("push", "origin", "clean-name", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert _blocked(result), result.stderr
    assert "commit " in result.stderr
    assert "git commit --amend" in result.stderr
    assert world.remote_branches(world.trace_free) == set()


def test_only_the_commits_the_push_adds_are_read(world: World) -> None:
    world.plain("checkout", "-q", "-b", "clean-name")
    world.commit("old\n\nCo-authored-by: Meiva #1")
    world.plain("push", "origin", "clean-name")
    world.plain("fetch", "-q", "origin")
    world.commit("new clean commit", name="g")
    result = world.git("push", "origin", "clean-name", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert result.returncode == 0, result.stderr


def test_a_trailer_below_the_tip_is_found(world: World) -> None:
    world.plain("checkout", "-q", "-b", "clean-name")
    world.commit("deep\n\nCo-authored-by: Meiva #8720", name="a")
    world.commit("clean one", name="b")
    world.commit("clean two", name="c")
    result = world.git("push", "origin", "clean-name", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert _blocked(result), result.stderr
    assert world.remote_branches(world.trace_free) == set()


def test_another_remotes_tracking_refs_do_not_hide_a_first_push(world: World) -> None:
    world.plain("checkout", "-q", "-b", "clean-name")
    world.commit("history\n\nCo-authored-by: Meiva #8720")
    world.plain("push", "other", "clean-name")
    world.plain("fetch", "-q", "other")
    assert world.plain("for-each-ref", "refs/remotes/origin/") == ""
    result = world.git("push", "origin", "clean-name", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert _blocked(result), result.stderr
    assert world.remote_branches(world.trace_free) == set()


def test_a_push_is_refused_when_nothing_can_run_the_guard(world: World, tmp_path: Path) -> None:
    git_dir = tmp_path / "only-git"
    git_dir.mkdir()
    (git_dir / "git").symlink_to(_machine_git())
    bare_path = f"{GIT_SHIM_DIR.resolve()}:{git_dir}"
    env = {**world.env({"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN}), "PATH": bare_path}
    env["MEIVA_PYTHON"] = str(tmp_path / "missing-python")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", *args], cwd=world.work, env=env, capture_output=True, text=True, check=False
        )

    refused = run("push", "origin", "HEAD:clean-name")
    assert refused.returncode == 1
    assert "refusing this push" in refused.stderr
    assert world.remote_branches(world.trace_free) == set()
    assert run("log", "-1", "--format=%s", "--grep=push").returncode == 0


def test_a_missing_meiva_python_falls_back_to_python3(world: World) -> None:
    world.plain("checkout", "-q", "-b", "meiva-topic")
    env = {"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN, "MEIVA_PYTHON": "/nonexistent/python"}
    result = world.git("push", "origin", "HEAD", **env)
    assert _blocked(result), result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ("--delete", "bad-8720"),
        ("-d", "bad-8720"),
        (":bad-8720",),
        ("--force", "--delete", "meiva-bad"),
    ],
)
def test_deleting_a_remote_branch_is_never_blocked(
    world: World, arguments: tuple[str, ...]
) -> None:
    for branch in ("bad-8720", "meiva-bad"):
        world.plain("push", "origin", f"main:{branch}")
    result = world.git(
        "push",
        "origin",
        *arguments,
        MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN,
        MEIVA_AGENT_ID=str(SELF_ID),
    )
    assert result.returncode == 0, result.stderr
    deleted = arguments[-1].removeprefix(":")
    assert deleted not in world.remote_branches(world.trace_free)


def test_a_push_without_arguments_follows_the_configured_remote_and_branch(world: World) -> None:
    world.plain("checkout", "-q", "-b", "meiva-topic")
    world.plain("config", "branch.meiva-topic.remote", "origin")
    world.plain("config", "branch.meiva-topic.merge", "refs/heads/meiva-topic")
    env = {"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN}
    assert _blocked(world.git("push", **env))
    world.plain("config", "branch.meiva-topic.pushRemote", "other")
    allowed = world.git("push", **env)
    assert allowed.returncode == 0, allowed.stderr
    assert "meiva-topic" in world.remote_branches(world.elsewhere)


def test_all_and_mirror_judge_every_branch_they_would_write(world: World) -> None:
    world.plain("branch", "agent-8720-old")
    env = {"MEIVA_ZERO_TRACE_GIT_REMOTES": PATTERN, "MEIVA_AGENT_ID": str(SELF_ID)}
    assert _blocked(world.git("push", "--all", "origin", **env))
    assert _blocked(world.git("push", "--mirror", "origin", **env))
    assert world.remote_branches(world.trace_free) == set()


def test_a_direct_url_is_matched_like_a_named_remote(world: World) -> None:
    world.plain("checkout", "-q", "-b", "meiva-x")
    result = world.git("push", str(world.trace_free), "HEAD", MEIVA_ZERO_TRACE_GIT_REMOTES=PATTERN)
    assert _blocked(result), result.stderr


@pytest.mark.parametrize(
    ("url", "patterns", "expected"),
    [
        ("https://github.com/example-corp/app.git", "example-corp/", True),
        ("git@github.com:example-corp/app.git", "example-corp/", True),
        ("ssh://git@github.com:22/example-corp/app", "example-corp/", True),
        ("git@github.com:example-corp/app.git", "github.com[:/]example-corp/", True),
        ("https://github.com/example-corp/app.git", "github.com[:/]example-corp/", True),
        ("https://github.com/Example-Corp/app.git", "example-corp/", True),
        ("https://github.com/other/example-corp-fork.git", "example-corp/", False),
        ("https://github.com/other/app.git", "example-corp/,another-org/", False),
        ("https://github.com/another-org/app.git", " example-corp/ , another-org/ ", True),
        ("https://github.com/example-corp/app.git", "", False),
        ("https://github.com/example-corp/app.git", " , ", False),
    ],
)
def test_pattern_matching_covers_https_and_ssh_urls(
    url: str, patterns: str, expected: bool
) -> None:
    assert guard.matches_zero_trace_remote([url], patterns) is expected


def test_the_exact_agent_id_needs_a_word_boundary() -> None:
    assert guard.branch_name_problem("fix-4367", 8720) is None
    assert guard.branch_name_problem("fix-87201", 8720) is None
    assert guard.branch_name_problem("x187200", 8720) is None
    for name in ("fix-8720", "8720-fix", "a/8720/b", "a_8720.b", "pr#8720"):
        assert guard.branch_name_problem(name, 8720) is not None, name
    assert guard.branch_name_problem("fix-8720", None) is None


def test_global_options_that_take_a_value_do_not_hide_the_subcommand() -> None:
    parsed = guard.parse_invocation(
        [
            "-C",
            "push",
            "-c",
            "x=push",
            "--git-dir=/a",
            "--work-tree",
            "/b",
            "--no-pager",
            "push",
            "-u",
        ]
    )
    assert parsed.subcommand == "push"
    assert parsed.args == ("-u",)
    assert parsed.replay == ("-C", "push", "-c", "x=push", "--git-dir=/a", "--work-tree", "/b")
    assert guard.parse_invocation(["-C", "d", "commit", "-m", "push"]).subcommand == "commit"
    assert guard.parse_invocation(["--version"]).subcommand is None


def test_push_arguments_are_read_as_git_reads_them() -> None:
    parsed = guard.parse_push(["-o", "ci.skip", "--repo", "r", "a:b", "-d"])
    assert (parsed.repository, parsed.refspecs, parsed.delete) == ("r", ("a:b",), True)
    assert guard.parse_push(["--del", "origin", "x"]).delete is True
    assert guard.parse_push(["-uf", "origin", "x"]).refspecs == ("x",)
    assert guard.parse_push(["--push-option", "p", "origin", "--", "-weird"]).refspecs == (
        "-weird",
    )
    assert guard.parse_push(["--recurse-submodules", "check", "origin"]).repository == "origin"
    assert guard.parse_push(["origin", "a", ":gone"]).refspecs == ("a", ":gone")
