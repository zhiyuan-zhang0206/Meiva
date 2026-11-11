# meiva:note
# Converge step: retire what WAL-G archiving left on a deployed host.
#
# Meiva no longer ships Postgres data off the machine, so nothing configures, schedules or runs
# WAL-G. A host that ran it earlier still has the pieces: the daily tick's launchd job or crontab
# entry with its log and heartbeat record, the tick's state directory, the pinned binary, and
# possibly a Postgres launched with `archive_mode=on`. This step removes them, idempotently, and
# leaves nothing to do on a host that never had them. The key file and the WAL-G configuration
# file an operator wrote live outside the home and are theirs; the objects in the bucket are not
# touched either.
#
# The binary is the one piece that waits: see `plans/walg_retirement.py`.

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from loguru import logger

from base.cluster.dataplane import pg_admin
from base.cluster.dataplane.runtime_binaries import runtime_root
from base.cluster.postgres import observe
from base.db.rows import query_one
from base.host.system.cron import mechanics
from base.host.system.job_values import LAUNCHD_LABEL_PREFIX, heartbeat_path, plist_path
from cli.commands.converge.plans.walg_retirement import (
    PostgresArchive,
    binary_may_go,
    deferral_message,
)
from cli.commands.converge.spec import ConvergeCtx

_LABEL = f"{LAUNCHD_LABEL_PREFIX}.walg"
_CRON_MARKER = "# meiva-walg"
_PROBE_TIMEOUT_S = 5


@dataclass(frozen=True)
class _ModeRow:
    mode: str


# meiva:note
# Whether the home's Postgres is running and what `archive_mode` it carries.
#
# A remote-managed data plane or a home without a registry record has no local Postgres of its
# own, so there is nothing running to archive. A postmaster that is running but cannot be asked
# comes back with `mode=None`.
def read_postgres_archive(ctx: ConvergeCtx) -> PostgresArchive:
    plane = ctx.read_config().view.data_plane
    try:
        authority = pg_admin.local_owner_authority(
            plane.db_url, remote_plane=plane.is_remote, home=ctx.meiva_home
        )
    except RuntimeError:
        return PostgresArchive(running=False, mode=None)
    try:
        if observe(authority.data_dir) is None:
            return PostgresArchive(running=False, mode=None)
        with pg_admin.connect(
            authority.admin_url,
            expected_data_dir=authority.data_dir,
            autocommit=True,
            connect_timeout=_PROBE_TIMEOUT_S,
        ) as conn:
            row = query_one(conn, _ModeRow, "SELECT current_setting('archive_mode') AS mode")
    except (RuntimeError, psycopg.Error):
        return PostgresArchive(running=True, mode=None)
    return PostgresArchive(running=True, mode=None if row is None else row.mode)


def _retire_job(ctx: ConvergeCtx) -> None:
    if not mechanics.owns_os_job_home("WAL-G tick", roots=ctx.job_roots):
        return
    host, roots = ctx.host, ctx.job_roots
    mechanics.unregister_platform_job(
        macos=lambda: (
            0
            if mechanics.remove_launchd_job(
                _LABEL, plist_path(_LABEL, user_home=roots.user_home), host=host
            )
            else 1
        ),
        linux=lambda: mechanics.remove_crontab_entry(
            _CRON_MARKER,
            write_failure_rc=1,
            on_removed=None,
            user_home=roots.user_home,
            host=host,
        ),
        host=host,
    )


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# meiva:note
# Remove the daily job and its files, then the binary once Postgres no longer archives.
def retire_walg(ctx: ConvergeCtx) -> None:
    execute_walg_retirement(ctx, archive=read_postgres_archive(ctx))


# meiva:note
# The effects, given what Postgres reported. Removing an absent job or file is a no-op, so a host
# that never ran WAL-G passes through untouched and a repeated converge changes nothing.
def execute_walg_retirement(ctx: ConvergeCtx, *, archive: PostgresArchive) -> None:
    _retire_job(ctx)
    home = ctx.meiva_home
    _remove(home / "logs" / "walg.log")
    _remove(heartbeat_path(home, "walg"))
    _remove(home / "backups" / "walg")
    binary_dir = runtime_root(home) / "walg"
    if not binary_dir.exists():
        return
    if binary_may_go(archive):
        _remove(binary_dir)
        logger.info("removed the retired wal-g binary at {}", binary_dir)
        return
    message = deferral_message(archive)
    logger.error("[walg] {}", message)
    print(f"  ! WAL-G retirement: {message}", file=sys.stderr)
