# meiva:note
# The untracked-migrations warning: the notice, and the step against a real git checkout.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands.converge.plans.checkout_warnings import untracked_migrations_notice
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host

_TRACKED = "20260101T000000_baseline-addition.sql"
_UNTRACKED = "20260808T010000_add-foo.sql"


# meiva:note
# --- the notice (pure) --------------------------------------------------------------


def test_the_notice_counts_and_lists_the_files_that_will_not_be_applied() -> None:
    notice = untracked_migrations_notice((_UNTRACKED, "20260809T010000_add-bar.sql"))

    assert notice is not None
    assert "2 untracked file(s) in migrations/ are NOT in git" in notice
    assert "will NOT be applied" in notice
    assert f"{_UNTRACKED}, 20260809T010000_add-bar.sql" in notice


def test_nothing_untracked_means_no_notice() -> None:
    assert untracked_migrations_notice(()) is None


# meiva:note
# --- the step against a real checkout -----------------------------------------------


# meiva:note
# A git checkout whose migrations/ tracks one file and holds `untracked` besides.
def _git_checkout_with_migrations(repo: Path, *, untracked: tuple[str, ...]) -> None:
    migrations = repo / "migrations"
    migrations.mkdir(parents=True)
    (migrations / _TRACKED).write_text("select 1;\n")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", f"migrations/{_TRACKED}"], check=True, capture_output=True
    )
    for name in untracked:
        (migrations / name).write_text("select 2;\n")


def test_warns_and_lists_untracked_files(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_checkout_with_migrations(ctx.repo, untracked=(_UNTRACKED,))

    converge_host.warn_untracked_migrations(ctx)

    out = capsys.readouterr().out
    assert "untracked" in out
    assert _UNTRACKED in out
    assert _TRACKED not in out
    assert "will NOT be applied" in out


def test_silent_when_nothing_untracked(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_checkout_with_migrations(ctx.repo, untracked=())

    converge_host.warn_untracked_migrations(ctx)

    assert capsys.readouterr().out == ""


# meiva:note
# The loader fails closed there and a warning would add nothing.
def test_silent_outside_a_git_worktree(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    (ctx.repo / "migrations").mkdir(parents=True)
    (ctx.repo / "migrations" / _UNTRACKED).write_text("select 2;\n")

    converge_host.warn_untracked_migrations(ctx)

    assert capsys.readouterr().out == ""


def test_registered_gateway_only() -> None:
    step = next(s for s in converge_host.CONVERGE_STEPS if s.name == "untracked migrations warning")
    assert step.roles == frozenset({"gateway"})
