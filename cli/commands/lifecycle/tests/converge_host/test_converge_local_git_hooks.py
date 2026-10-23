# meiva:note
# The local Git hook drift warning: what the scan's output means, and that it never blocks.

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import cli.commands.converge.brew_pin as cbp
import cli.commands.converge.steps as csteps
import cli.commands.lifecycle.converge_host as cv
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, fake_host
from cli.commands.converge.plans.checkout_warnings import hook_scan_argv, hook_scan_warnings
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx

_DRIFT = (
    "WARNING: [/home/u/Meiva] pre-commit: INSTALL_PYTHON points into a disposable worktree: /x\n"
    "hook check: 1 problem(s) across 1 clone(s)\n"
)

# meiva:note
# --- what a scan's output means (pure) ----------------------------------------------


def test_the_scan_runs_the_checkers_machine_mode_with_the_given_interpreter() -> None:
    script = Path("/repo/base/deploy/git/check_git_hooks.py")

    assert hook_scan_argv("/venv/bin/interpreter", script) == (
        "/venv/bin/interpreter",
        str(script),
        "--scan-machine",
    )


@pytest.mark.parametrize("returncode", [0, 1])
def test_warning_lines_are_mirrored_when_the_scan_ran(returncode: int) -> None:
    assert hook_scan_warnings(returncode, _DRIFT) == (
        "  ! hooks: [/home/u/Meiva] pre-commit: INSTALL_PYTHON points into a disposable "
        "worktree: /x",
    )


def test_a_clean_machine_has_no_warning_lines() -> None:
    assert hook_scan_warnings(0, "hook check: OK (3 clone(s))\n") == ()


# meiva:note
# A checkout that predates the flag answers a usage error: stay silent, never noisy.
@pytest.mark.parametrize("returncode", [2, 3, -9])
def test_an_unexpected_exit_is_silence_even_when_it_printed_warnings(returncode: int) -> None:
    assert hook_scan_warnings(returncode, _DRIFT) == ()
    assert hook_scan_warnings(returncode, "usage: check_git_hooks.py [-h] [--scan-machine]\n") == ()


# meiva:note
# --- the step against a real checker script ------------------------------------------


# meiva:note
# A context whose checkout carries a checker that prints `stdout` and exits `code`.
#
# The checker insists on the `--scan-machine` flag, so a step that ran it any other way would
# get the drift warning below instead of the silence the clean case expects.
def _checkout_with_checker(ctx: ConvergeCtx, *, stdout: str, code: int) -> ConvergeCtx:
    script = ctx.repo / "base" / "deploy" / "git" / "check_git_hooks.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\n"
        "if sys.argv[1:] != ['--scan-machine']:\n"
        "    print('WARNING: unexpected arguments')\n"
        "    sys.exit(1)\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({code})\n"
    )
    # meiva:note
    # host-ok: the scan runs as a real child process; nothing here depends on the platform
    return replace(ctx, host=Host.system())


def test_step_is_registered_after_brew_pin() -> None:
    steps = cv.CONVERGE_STEPS
    hooks_index = next(
        i for i, step in enumerate(steps) if step.apply is csteps.ensure_local_git_hooks
    )
    brew_index = next(i for i, step in enumerate(steps) if step.apply is cbp.ensure_brew_pin)
    assert hooks_index > brew_index
    step = steps[hooks_index]
    assert step.roles == ALL_ROLES
    assert step.requires_unit_config is False


def test_clean_machine_is_silent(ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]) -> None:
    checked = _checkout_with_checker(ctx, stdout="hook check: OK (3 clone(s))\n", code=0)

    csteps.ensure_local_git_hooks(checked)

    assert capsys.readouterr().err == ""


def test_drifted_machine_warns(ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]) -> None:
    checked = _checkout_with_checker(ctx, stdout=_DRIFT, code=1)

    csteps.ensure_local_git_hooks(checked)

    err = capsys.readouterr().err
    assert err.startswith("  ! hooks: ")
    assert "disposable worktree" in err


def test_missing_script_is_silent(ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]) -> None:
    # meiva:note
    # The fake host answers no command, so a scan that ran anyway would fail the step.
    csteps.ensure_local_git_hooks(ctx)

    assert capsys.readouterr().err == ""


def test_usage_error_from_pre_flag_checkout_is_silent(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    checked = _checkout_with_checker(
        ctx, stdout="usage: check_git_hooks.py [-h] [--scan-machine]\n", code=2
    )

    csteps.ensure_local_git_hooks(checked)

    assert capsys.readouterr().err == ""


def test_a_checker_that_fails_unexpectedly_is_silent(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    checked = _checkout_with_checker(ctx, stdout=_DRIFT, code=3)

    csteps.ensure_local_git_hooks(checked)

    assert capsys.readouterr().err == ""


# meiva:note
# Faults a real checker cannot produce on demand: the interpreter vanishing, a hung scan.
@pytest.mark.parametrize(
    "fault",
    [OSError("git vanished"), subprocess.TimeoutExpired(cmd="check_git_hooks.py", timeout=30)],
    ids=["os-error", "timeout"],
)
def test_a_scan_that_cannot_run_is_silent(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str], fault: BaseException
) -> None:
    script = ctx.repo / "base" / "deploy" / "git" / "check_git_hooks.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    commands = FakeCommands()
    commands.on([sys.executable], fault)

    csteps.ensure_local_git_hooks(replace(ctx, host=fake_host(commands)))

    assert capsys.readouterr().err == ""
