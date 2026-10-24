# meiva:note
# The bare `meiva` on PATH belongs to the host's prod install, wired through `converge_host`.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime

_CLI_LINK_STEPS = tuple(
    step for step in converge_host.CONVERGE_STEPS if step.name == "meiva on PATH"
)


# meiva:note
# Converge the `meiva on PATH` step of a unit whose checkout is `<home>/source` by default.
def _first_start_converge(
    ctx: ConvergeCtx,
    command_runtime: CommandRuntime,
    user_home: Path,
    *,
    meiva_home: Path,
    existing_target: Path | None = None,
    checkout: Path | None = None,
) -> tuple[Path, Path]:
    checkout = meiva_home / "source" if checkout is None else checkout
    checkout.mkdir(parents=True)
    bare_link = user_home / ".local" / "bin" / "meiva"
    if existing_target is not None:
        bare_link.parent.mkdir(parents=True)
        bare_link.symlink_to(existing_target)
    converge_host.converge_host(
        checkout,
        frozenset({"gateway", "agent-runner"}),
        steps=_CLI_LINK_STEPS,
        services=frozenset(),
        database_factory=ctx.database_factory,
        producer=ctx.producer,
        command=replace(command_runtime, home=meiva_home),
    )
    return checkout, bare_link


def test_prod_first_start_links_bare_meiva_to_the_checkout_cli(
    ctx: ConvergeCtx, command_runtime: CommandRuntime, default_home: Path
) -> None:
    meiva_home = default_home / ".meiva"

    checkout, bare_link = _first_start_converge(
        ctx, command_runtime, default_home, meiva_home=meiva_home
    )

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "meiva"
    assert not (meiva_home / "meiva").exists() and not (meiva_home / "meiva").is_symlink()


def test_prod_start_repoints_the_retired_launcher_link(
    ctx: ConvergeCtx, command_runtime: CommandRuntime, default_home: Path
) -> None:
    meiva_home = default_home / ".meiva"
    retired = meiva_home / "source" / "scripts" / "meiva-launcher.sh"

    checkout, bare_link = _first_start_converge(
        ctx, command_runtime, default_home, meiva_home=meiva_home, existing_target=retired
    )

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "meiva"


def test_non_prod_first_start_leaves_the_host_link_alone(
    ctx: ConvergeCtx, command_runtime: CommandRuntime, default_home: Path
) -> None:
    prod_cli = default_home / "prod" / "source" / ".venv" / "bin" / "meiva"
    meiva_home = default_home / ".meiva-preview"

    _, bare_link = _first_start_converge(
        ctx, command_runtime, default_home, meiva_home=meiva_home, existing_target=prod_cli
    )

    assert bare_link.readlink() == prod_cli
    assert not (meiva_home / "meiva").exists() and not (meiva_home / "meiva").is_symlink()


def test_non_prod_first_start_creates_no_bare_link_when_none_existed(
    ctx: ConvergeCtx, command_runtime: CommandRuntime, default_home: Path
) -> None:
    _, bare_link = _first_start_converge(
        ctx, command_runtime, default_home, meiva_home=default_home / ".meiva-preview"
    )

    assert not bare_link.exists()
    assert not bare_link.is_symlink()


# meiva:note
# An uninstalled worktree may resolve to ~/.meiva; its repository still makes it a dev
# cluster and it must never repoint the production symlink.
@pytest.mark.parametrize("parent", [".claude/worktrees", ".worktrees"])
def test_a_dev_worktree_never_links_the_host_even_when_its_home_is_the_default(
    parent: str, ctx: ConvergeCtx, command_runtime: CommandRuntime, default_home: Path
) -> None:
    worktree = default_home / "Meiva" / parent / "feature"

    _, bare_link = _first_start_converge(
        ctx, command_runtime, default_home, meiva_home=default_home / ".meiva", checkout=worktree
    )

    assert not bare_link.exists()
    assert not bare_link.is_symlink()
