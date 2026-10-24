# meiva:note
# The scheduler daemon of the shutdown tests, as its own process: the production `run`
# over a home of its own, a real health server on a free port, a real pidfile, and a
# launch that names the blocking worker of `shutdown_worker.py`. The parent test sends it
# SIGTERM and inspects what is left.
#
# Usage: `shutdown_daemon.py <home> <mode> <throwaway base>`.

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

from base.config import ConfigBoot, ensure_eager
from base.host.env import port_lease
from base.native_process.child_env import inherited_process_env
from base.native_process.loaded_commit import LoadedCommit
from services.backup.layout import BackupTree
from services.backup.scheduler import daemon
from services.backup.scheduler.settings import SchedulerSettings
from services.backup.scheduler.worker import WorkerLaunch

_WORKER = "services.backup.scheduler.tests.shutdown_worker"


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def main(root: Path, mode: str, postgres_base: Path) -> None:
    ensure_eager()
    config = ConfigBoot()
    config.view.general.timezone = "UTC"
    config.view.services.backup_hour = 0
    config.view.services.pg_backup_health_port = _free_port()
    # meiva:note
    # A relative base exercises the settings' own absolute-path capture.
    config.view.data_plane.pg_throwaway_base = str(Path(os.path.relpath(postgres_base, root)))
    launch = WorkerLaunch(
        _WORKER,
        {
            **inherited_process_env(),
            "MEIVA_TEST_BACKUP_ROOT": str(root),
            "MEIVA_TEST_BACKUP_MODE": mode,
            "MEIVA_TEST_BACKUP_POSTGRES_BASE": str(postgres_base),
        },
    )
    daemon.install_graceful_shutdown("backup-shutdown-test")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            daemon.run(
                config=config,
                image=LoadedCommit(Path(), None),
                runtime=root / "run",
                tree=BackupTree(root),
                launch=launch,
                settings=SchedulerSettings.read(config),
            )
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
