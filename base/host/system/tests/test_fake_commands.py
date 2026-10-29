from __future__ import annotations

import ast
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from base.host.proc import CommandRunner
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.paths import repo_root


def test_it_is_a_command_runner_that_records_each_call() -> None:
    commands = FakeCommands()
    commands.on("git", Reply(stdout="ok\n"), repeat=True)
    runner: CommandRunner = commands
    runner(["git", "status"], timeout=5, capture_output=True, text=True, cwd="/work")
    runner(["git", "log"], timeout=7, input="in")
    assert commands.argvs == [("git", "status"), ("git", "log")]
    first, second = commands.calls
    assert first.timeout == 5
    assert first.kwargs == {"capture_output": True, "input": None, "text": True, "cwd": "/work"}
    assert second.kwargs["input"] == "in"


def test_replies_are_consumed_in_order_and_the_last_repeats_only_when_asked() -> None:
    commands = FakeCommands()
    commands.on("a", Reply(returncode=1), Reply(returncode=2))
    commands.on("b", Reply(returncode=3), Reply(returncode=4), repeat=True)
    assert [commands(["a"], timeout=1).returncode for _ in range(2)] == [1, 2]
    with pytest.raises(AssertionError, match="ran out"):
        commands(["a"], timeout=1)
    assert [commands(["b"], timeout=1).returncode for _ in range(4)] == [3, 4, 4, 4]


def test_the_first_matching_prefix_wins_and_an_empty_prefix_matches_everything() -> None:
    commands = FakeCommands()
    commands.on(["launchctl", "print"], Reply(stdout="specific"), repeat=True)
    commands.on([], Reply(stdout="anything"), repeat=True)
    assert (
        commands(["launchctl", "print", "x"], timeout=1, capture_output=True, text=True).stdout
        == "specific"
    )
    assert (
        commands(["launchctl", "list"], timeout=1, capture_output=True, text=True).stdout
        == "anything"
    )


def test_an_unscripted_command_fails_instead_of_answering_with_a_default() -> None:
    commands = FakeCommands()
    commands.on("lsof", Reply(), repeat=True)
    with pytest.raises(AssertionError, match=r"unscripted command: \['ps', '-p', '1'\]"):
        commands(["ps", "-p", "1"], timeout=1)


def test_a_scripted_exception_is_raised_like_the_real_failure() -> None:
    commands = FakeCommands()
    commands.on("slow", subprocess.TimeoutExpired("slow", 10))
    commands.on("gone", FileNotFoundError("gone"))
    with pytest.raises(subprocess.TimeoutExpired):
        commands(["slow"], timeout=10)
    with pytest.raises(FileNotFoundError):
        commands(["gone"], timeout=10)


def test_streams_are_none_unless_captured_and_follow_the_callers_text_mode() -> None:
    commands = FakeCommands()
    commands.on("x", Reply(stdout="out", stderr="err"), repeat=True)
    bare = commands(["x"], timeout=1)
    assert (bare.stdout, bare.stderr) == (None, None)
    as_text = commands(["x"], timeout=1, capture_output=True, text=True)
    assert (as_text.stdout, as_text.stderr) == ("out", "err")
    as_bytes = commands(["x"], timeout=1, capture_output=True)
    assert (as_bytes.stdout, as_bytes.stderr) == (b"out", b"err")
    only_out = commands(["x"], timeout=1, stdout=subprocess.PIPE, text=True)
    assert (only_out.stdout, only_out.stderr) == ("out", None)


def test_the_completed_process_carries_the_argv_and_the_exit_code() -> None:
    commands = FakeCommands()
    commands.on("lsof", Reply(returncode=1), repeat=True)
    done = commands(["lsof", "-nP"], timeout=10)
    assert done.args == ["lsof", "-nP"]
    assert done.returncode == 1


def test_a_route_without_replies_is_a_test_bug() -> None:
    with pytest.raises(ValueError, match="at least one reply"):
        FakeCommands().on("x")


def test_a_predicate_selects_commands_whose_program_is_not_known_up_front() -> None:
    commands = FakeCommands()
    commands.on(lambda argv: "-verify-config" in argv, Reply(stdout="verified"), repeat=True)
    done = commands(
        ["/home/meiva/lgtm/loki", "-config.file=x", "-verify-config"],
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert done.stdout == "verified"
    with pytest.raises(AssertionError, match="unscripted"):
        commands(["/home/meiva/lgtm/loki", "-version"], timeout=30)


def test_a_fake_host_finds_only_the_programs_it_was_given_and_runs_nothing_unscripted() -> None:
    host = fake_host(programs={"gh": "/usr/bin/gh"}, platform=DARWIN_ARM64, uid=501)
    assert host.which("gh") == "/usr/bin/gh"
    assert host.which("lsof") is None
    assert (host.platform, host.uid) == (DARWIN_ARM64, 501)
    with pytest.raises(AssertionError, match="unscripted"):
        host.run(["gh", "auth", "status"], timeout=5)
    assert fake_host().platform == LINUX_X86


# meiva:note
# The identity a test host impersonates comes from the process running the test. A canned
# default agrees with the files a test makes only on a machine whose uid happens to be that
# constant, which is how #7529 and #7648 passed locally and failed on CI.
def test_a_fake_host_is_owned_by_the_user_running_the_test_unless_it_is_told_otherwise() -> None:
    assert fake_host().uid == os.geteuid()
    assert fake_host(uid=501).uid == 501


# meiva:note
# The guard against writing that trap again: no test double may pin an identity it should
# derive. A fixture whose parameter *is* the behaviour under test says so here, with why.
#
# What it cannot see: a *call site* that omits the argument and then asserts a literal
# identity (`launchctl bootstrap gui/1000 ...`). That keeps passing here and fails only
# where the machine's uid differs — `autostart/test_registration.py` was exactly that
# (2026-10-29). Deriving a default moves the risk from the signature to the literal, so
# a scan of signatures says nothing about the assertions around them.
_PINNED_IDENTITY: dict[tuple[str, str, str], str] = {
    ("base/host/system/tests/test_boot_unit.py", "_linux", "uid"): (
        "every caller but one exercises the root-only branch of the boot unit; a derived uid "
        "would select the unprivileged branch instead"
    ),
}
_IDENTITY_PARAMETERS = frozenset({"uid", "euid", "hostname", "host_name", "os_user", "user_name"})
_SKIPPED_DIRECTORIES = frozenset({"__pycache__", "node_modules"})


def _test_modules(repo: Path) -> Iterator[Path]:
    for current, directories, files in os.walk(repo):
        directories[:] = sorted(
            d for d in directories if d not in _SKIPPED_DIRECTORIES and not d.startswith(".")
        )
        if "tests" not in Path(current).relative_to(repo).parts:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(current) / name


def _pinned_identity_defaults(tree: ast.AST) -> Iterator[tuple[str, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        filled: list[ast.expr | None] = [None] * (len(positional) - len(node.args.defaults))
        pairs = [*zip(positional, [*filled, *node.args.defaults], strict=True)]
        pairs += [*zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)]
        for argument, default in pairs:
            if argument.arg not in _IDENTITY_PARAMETERS or default is None:
                continue
            if isinstance(default, ast.Constant) and default.value is None:
                continue
            yield node.name, argument.arg


def test_no_test_double_pins_an_identity_it_should_derive() -> None:
    repo = repo_root()
    pinned: list[tuple[str, str, str]] = []
    for path in _test_modules(repo):
        source = path.read_text(encoding="utf-8")
        if "fake_host(" not in source and "Host(" not in source:
            continue
        for function, parameter in _pinned_identity_defaults(ast.parse(source)):
            pinned.append((path.relative_to(repo).as_posix(), function, parameter))

    unexplained = [entry for entry in pinned if entry not in _PINNED_IDENTITY]
    assert unexplained == [], (
        "a test double pins an identity the running user owns; derive it (uid=None) and let "
        f"the caller pass a specific account, or record why the constant is the point: {unexplained}"
    )
