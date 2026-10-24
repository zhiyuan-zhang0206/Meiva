# meiva:note
# What the WAL-G converge step does for this cluster.
#
# A pure decision over whether WAL-G is switched on (`MEIVA_WALG_CONFIG_FILE` is set) and
# whether the cluster's Postgres is remote-managed. The shell in `walg.py` runs the steps.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "InstallWalgBinary",
    "LoadWalgConfig",
    "PrepareWalgStateDir",
    "RefuseWalg",
    "WalgStep",
    "plan_walg_converge",
]


# meiva:note
# Install the pinned binary at its stable path.
@dataclass(frozen=True)
class InstallWalgBinary: ...


# meiva:note
# Validate the configuration and pin its key fingerprint on first use.
@dataclass(frozen=True)
class LoadWalgConfig: ...


# meiva:note
# Make the private state directory under the home.
@dataclass(frozen=True)
class PrepareWalgStateDir: ...


# meiva:note
# WAL-G cannot run here; converge fails with this message.
@dataclass(frozen=True)
class RefuseWalg:
    message: str


WalgStep = InstallWalgBinary | LoadWalgConfig | PrepareWalgStateDir | RefuseWalg


# meiva:note
# Nothing while WAL-G is off; otherwise the steps that make it safe before Postgres starts.
#
# With the key unset every part is a no-op: a development home installs nothing and registers
# nothing. A remote-managed Postgres cannot archive WAL through a local `archive_command`.
def plan_walg_converge(*, configured: bool, remote_plane: bool) -> tuple[WalgStep, ...]:
    if not configured:
        return ()
    if remote_plane:
        return (
            RefuseWalg(
                "MEIVA_WALG_CONFIG_FILE is set but this cluster's Postgres is remote-managed: "
                "WAL archiving needs the locally launched Postgres (unset the key)"
            ),
        )
    return (InstallWalgBinary(), LoadWalgConfig(), PrepareWalgStateDir())
