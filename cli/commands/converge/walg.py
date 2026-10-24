"""Converge step: install and validate WAL-G when MEIVA_WALG_CONFIG_FILE switches it on.

Runs before Postgres starts (right after its runtime step), so a broken
configuration fails `meiva start` here instead of becoming a Postgres whose archive
command can never succeed. With the key unset every part of this is a no-op: a
development home installs nothing and registers nothing.
"""

from __future__ import annotations

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


def converge_walg(ctx: ConvergeCtx) -> None:
    """Install the pinned binary, validate the configuration, pin the key on first use.

    Raises:
        RuntimeError: the data plane is remote-managed, this platform has no pinned
            wal-g, the download failed its checksum, or the configuration is unusable
            (`WalgConfigError` names the file and the setting, never a value).
    """
    configured = ctx.read_config().view.walg.walg_config_file is not None
    remote_plane = configured and ctx.read_config().view.data_plane.is_remote
    execute_walg_plan(plan_walg_converge(configured=configured, remote_plane=remote_plane), ctx)


# meiva:note
# Run `plan` in order; the first failing step raises.
def execute_walg_plan(plan: tuple[WalgStep, ...], ctx: ConvergeCtx) -> None:
    for step in plan:
        match step:
            case RefuseWalg(message=message):
                raise RuntimeError(message)
            case InstallWalgBinary():
                ensure_walg_binary()
            case LoadWalgConfig():
                walg_config.load_walg_config(path=ctx.read_config().view.walg.walg_config_file)
            case PrepareWalgStateDir():
                ensure_private_dir(ctx.meiva_home / "backups" / "walg")
