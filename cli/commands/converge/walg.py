"""Converge step: install and validate WAL-G when MEIVA_WALG_CONFIG_FILE switches it on.

Runs before Postgres starts (right after its runtime step), so a broken
configuration fails `meiva start` here instead of becoming a Postgres whose archive
command can never succeed. With the key unset every part of this is a no-op: a
development home installs nothing and registers nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from base.cluster.dataplane.walg_binary import ensure_walg_binary
from base.paths.private_dir import ensure_private_dir
from cli.commands.converge.plans.walg_plan import (
    InstallWalgBinary,
    LoadWalgConfig,
    PrepareWalgStateDir,
    RefuseWalg,
    WalgStep,
    plan_walg_converge,
)
from cli.commands.converge.spec import ConvergeCtx
from services.backup.walg import config as walg_config
from services.backup.walg import state as walg_state


def converge_walg(ctx: ConvergeCtx) -> None:
    """Install the pinned binary, validate the configuration, pin the key on first use.

    Raises:
        RuntimeError: the data plane is remote-managed, this platform has no pinned
            wal-g, the download failed its checksum, or the configuration is unusable
            (`WalgConfigError` names the file and the setting, never a value).
    """
    view = ctx.read_config().view
    walg_config_file: Path | None = view.walg.walg_config_file
    configured = walg_config_file is not None
    remote_plane = configured and view.data_plane.is_remote
    execute_walg_plan(
        plan_walg_converge(configured=configured, remote_plane=remote_plane),
        ctx,
        walg_config_file=walg_config_file,
    )


# meiva:note
# Run `plan` in order; the first failing step raises.
#
# The services' WAL-G API takes the configuration path as a reader, so the already-read path is
# wrapped once, at the one step that needs it.
def execute_walg_plan(
    plan: tuple[WalgStep, ...], ctx: ConvergeCtx, *, walg_config_file: Path | None
) -> None:
    for step in plan:
        match step:
            case RefuseWalg(message=message):
                raise RuntimeError(message)
            case InstallWalgBinary():
                ensure_walg_binary(home=ctx.meiva_home)
            case LoadWalgConfig():
                walg_config.load_walg_config(path=walg_config_file, home=ctx.meiva_home)
            case PrepareWalgStateDir():
                ensure_private_dir(walg_state.walg_dir(ctx.meiva_home), euid=ctx.host.uid)
            case _:
                assert_never(step)
