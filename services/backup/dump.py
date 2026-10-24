# meiva:note
# Daily local Postgres backup, driven by the gateway scheduler daemon.
#
# One `pg_dump --format=custom` per day into `$MEIVA_HOME/backups/db/`,
# keeping the newest ``backup_keep`` dumps (``services.backup_keep``, default 7).
# `services.backup.scheduler.daemon` asks `services.backup.schedule.backup_due`
# independently of watchdog rounds: at the first wake after ``backup_hour`` cluster
# time with no dump for the current cluster day, so a host that was down at 03:00
# catches up. Its operation worker runs `run_backup(staging=...)` inside private
# controls; the controller links that artifact into the backup directory after the
# worker's group closed (`services.backup.scheduler.worker`).
#
# Backup cadence follows the configured cluster timezone; artifact names carry
# UTC timestamps. Retention orders those timestamps independently of host DST.
#
# Local dumps guard against bad migrations / accidental deletes / DB
# corruption. `run_backup` is `dump -> encrypt -> optional off-site publish ->
# prune`. The best-effort off-site leg publishes the encrypted artifact to OSS
# iff absent (`services.backup.artifact.offsite`); an unconfigured,
# unavailable or failing store keeps the local artifact, and nothing here deletes
# a remote object (see `future/infra/data/pg-backup.md`). The dump uses PostgreSQL's
# compressed custom format.
#
# The LangGraph checkpoint tables (`checkpoint_blobs`, `checkpoints`, and
# `checkpoint_writes`) are the only copy of conversation history: messages, tool
# outputs, and compaction segments all live there. Every daily dump includes them.
# The custom dump is encrypted before publication, so local and optional off-site
# artifacts contain the complete recoverable database without storing plaintext
# conversation data at rest.
#
# Only files matching `services.backup.artifact.names` are managed (counted
# for due-ness, pruned); a hand-made dump parked in the same directory is never
# touched.
#
# Restore procedure: `.agents/skills/meiva-guide/operations/references/db-restore.md`.

from __future__ import annotations

import argparse
import logging
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from base.cluster.dataplane.pg_admin import local_owner_authority
from base.cluster.dataplane.pg_tools import has_pg_tool as has_pg_tool
from base.cluster.dataplane.pg_tools import pg_tool
from base.db import Database, connect_url
from base.host.private_storage import ensure_private_file
from base.native_process.child_env import inherited_process_env
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.paths.private_dir import ensure_private_dir
from services.backup.artifact import passphrase as backup_passphrase
from services.backup.artifact.intermediates import sweep_closed_partials
from services.backup.artifact.names import DUMP_NAME_RE, REMOTE_ROOT, stamp_utc
from services.backup.artifact.offsite_plan import OssTarget
from services.backup.dump_lines import beat_line, size_breakdown, started_line
from services.backup.dump_plan import (
    DumpFiles,
    decrypt_argv,
    encrypt_argv,
    pg_dump_argv,
    pg_dump_env,
    prune_victims,
    target_files,
)
from services.backup.layout import BackupTree

_log = logging.getLogger(__name__)

# Headroom against a stall, not an expected runtime: a full dump with
# checkpoint history takes about 6.3 min.
DUMP_TIMEOUT_S = 60 * 60
# Heartbeat cadence while a dump or an encryption runs with a progress sink
# attached. Either may run for many minutes without writing anything; a beat
# every 60 s keeps the sink's operator's view alive instead of reading a healthy
# dump as a hung one.
PROGRESS_INTERVAL_S = 60.0
# Bound the composition-sample connection (a dead DB must stall the backup log
# line only this long before degrading to "unavailable", never hang it).
_BREAKDOWN_CONNECT_TIMEOUT_S = 10
_backup_lock_guard = threading.RLock()
_backup_lock_state = threading.local()


@contextmanager
def backup_lock(tree: BackupTree, *, timeout_s: float | None = None) -> Generator[None]:
    """Serialize backup creation and verification across local processes.

    The lock is re-entrant within one thread, so a caller holding it can call
    `run_backup`.
    """
    with _backup_lock_guard:
        depth = getattr(_backup_lock_state, "depth", 0)
        if depth:
            _backup_lock_state.depth = depth + 1
            try:
                yield
            finally:
                _backup_lock_state.depth -= 1
            return

        with file_lock(tree.lock, timeout_s=timeout_s):
            _backup_lock_state.depth = 1
            try:
                yield
            finally:
                del _backup_lock_state.depth


def managed_dumps(directory: Path) -> list[tuple[datetime, Path]]:
    """This module's dumps in `directory`, oldest first, keyed by UTC instant."""
    dumps: list[tuple[datetime, Path]] = []
    if not directory.exists():
        return dumps
    for path in directory.iterdir():
        m = DUMP_NAME_RE.match(path.name)
        if m and path.is_file():
            dumps.append((stamp_utc(m["ts"]), path))
    return sorted(dumps)


# meiva:note
# The UTC instant of the newest managed dump in `directory`, or None when there is none.
def newest_dump_instant(directory: Path) -> datetime | None:
    dumps = managed_dumps(directory)
    return dumps[-1][0] if dumps else None


# meiva:note
# Delete managed dumps beyond retention: all but the newest `keep`.
def prune(directory: Path, *, keep: int) -> list[Path]:
    removed = prune_victims(managed_dumps(directory), keep)
    for path in removed:
        path.unlink()
    return removed


# meiva:note
# The dial `pg_dump` reads this cluster's whole database through.
#
# A locally owned plane dumps as the administrator acting as the schema owner
# over the home's owner-only socket (`base.cluster.dataplane.pg_admin`): password-free,
# custody-checked against this home's postmaster, and independent of the
# write generation, so a dump never needs, and never dies with, a delivered
# login. A remote-managed plane's provider URL
# (`Database.direct_url`) is its only authority; `passwordless_conninfo` keeps its
# password off argv. Both bypass PgBouncer: pg_dump holds one snapshot across
# many statements, which a transaction pooler cannot keep.
def dump_source(db: Database, *, is_remote: bool, plane_url: str) -> str:
    if is_remote:
        return db.direct_url()
    return local_owner_authority(plane_url, remote_plane=False).verified_conninfo()


# meiva:note
# Return pg_dump conninfo and password separately so the latter never enters argv.
def passwordless_conninfo(db_url: str) -> tuple[str, str]:
    parsed = conninfo_to_dict(db_url)
    # meiva:note
    # Preserve SSL and other connection settings.  Password is the sole field
    # deliberately split into the child-only environment below.
    fields = {key: str(value) for key, value in parsed.items() if key != "password"}
    conninfo = make_conninfo(**fields)
    password = parsed.get("password")
    return conninfo, password if isinstance(password, str) else ""


def decrypt_artifact(artifact: Path, custom_dump: Path, *, tree: BackupTree) -> None:
    """Decrypt one managed artifact into its raw `pg_dump --format=custom` archive.

    The caller owns `custom_dump` and removes it once consumed. The pinned
    passphrase decrypts, never placed on argv.
    """
    custom_dump.touch(mode=0o600, exist_ok=False)
    custom_dump.chmod(0o600)
    key_file = backup_passphrase.write_key_file(custom_dump.parent, tree.home)
    try:
        proc = subprocess.run(  # noqa: S603
            decrypt_argv(key_file, artifact, custom_dump),
            capture_output=True,
            check=False,
            env=inherited_process_env(),
            timeout=DUMP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"backup decrypt exited {proc.returncode}")
    finally:
        with suppress(OSError):
            key_file.unlink(missing_ok=True)


# A one-line progress report, called at most every `PROGRESS_INTERVAL_S` while a
# long, otherwise-silent pipeline stage runs (see `run_with_progress`).
_ProgressSink = Callable[[str], None]


# meiva:note
# Bytes a child's output file holds — None when it cannot be read.
def _written_bytes(size_path: Path | None) -> int | None:
    if size_path is None:
        return None
    try:
        return size_path.stat().st_size
    except OSError:
        return None


# meiva:note
# `subprocess.run(argv, capture_output=True, check=False)` that narrates its wait.
#
# With `progress=None` this is exactly `subprocess.run`. With a sink, one line
# names the child's bound and one every `interval_s` carries the
# elapsed time and the bytes `size_path` holds, so a caller's operator sees a
# long silent dump or encryption alive until its own `timeout_s`.
#
# Like `subprocess.run`, expiry or any interruption kills and reaps the child
# before raising, so its output never has a live writer afterwards.
def run_with_progress(
    argv: list[str],
    *,
    timeout_s: float,
    label: str,
    progress: _ProgressSink | None,
    env: Mapping[str, str],
    size_path: Path | None = None,
    interval_s: float = PROGRESS_INTERVAL_S,
) -> subprocess.CompletedProcess[bytes]:
    if progress is None:
        return subprocess.run(  # noqa: S603
            argv, capture_output=True, check=False, env=dict(env), timeout=timeout_s
        )
    started = time.monotonic()
    progress(started_line(label, timeout_s))
    with subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(env)
    ) as proc:
        try:
            stdout, stderr = _narrated_wait(
                proc, argv, timeout_s, started, (label, progress, size_path), interval_s
            )
        except BaseException:
            proc.kill()
            raise  # `Popen.__exit__` reaps it.
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _narrated_wait(
    proc: subprocess.Popen[bytes],
    argv: list[str],
    timeout_s: float,
    started: float,
    narration: tuple[str, _ProgressSink, Path | None],
    interval_s: float,
) -> tuple[bytes, bytes]:
    label, progress, size_path = narration
    while True:
        remaining_s = timeout_s - (time.monotonic() - started)
        if remaining_s <= 0:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        try:
            return proc.communicate(timeout=min(interval_s, remaining_s))
        except subprocess.TimeoutExpired:
            progress(beat_line(label, time.monotonic() - started, _written_bytes(size_path)))


# meiva:note
# Dump the cluster DB into the managed pool and prune; return the dump path.
#
# Plaintext and encrypted intermediates use `.partial` names; only the
# encrypted custom-format artifact is published after every pipeline step
# succeeds. A failed step removes its intermediates once its tool is reaped.
# `staging` writes into a scheduled operation's private controls instead
# and leaves publication and pruning to that operation's controller.
#
# `timeout_s` lets bounded callers use a tighter ceiling than the daily
# backup default. `offsite_target=None` keeps the completed artifact local, so
# off-site network latency cannot extend the run.
#
# `progress` narrates the stages that may run for minutes without writing
# anything (`pg_dump` and the encryption pass) — see `run_with_progress`.
def run_backup(
    now: datetime,
    *,
    tree: BackupTree,
    db_url: str,
    keep: int,
    offsite_target: OssTarget | None,
    staging: Path | None = None,
    timeout_s: float = DUMP_TIMEOUT_S,
    progress: _ProgressSink | None = None,
    progress_interval_s: float = PROGRESS_INTERVAL_S,
) -> Path:
    with backup_lock(tree):
        directory = tree.managed if staging is None else staging
        # Every run, the scheduled worker's staged one included, first clears
        # the backup directory of intermediates whose writers closed: a killed
        # in-process snapshot's plaintext never waits for another snapshot.
        for swept in dict.fromkeys((tree.managed, directory)):
            sweep_closed_partials(ensure_private_dir(swept))
        target = run_locked_backup(
            now,
            tree=tree,
            db_url=db_url,
            directory=directory,
            bound=_Bound(timeout_s, progress, progress_interval_s),
        )
        if offsite_target is not None:
            # Imported where used: the scheduler daemon imports this module for
            # meiva:note
            # its schedule and must not carry the OSS SDK; its backup worker, which
            # reaches this line, loads it.
            from services.backup.artifact import offsite

            offsite.publish(target, offsite_target)
        if staging is None:
            _log_written(target, prune(target.parent, keep=keep))
        return target


def prune_after_publish(target: Path, *, tree: BackupTree, keep: int) -> None:
    """Prune around a newly linked scheduled dump without waiting on the lock.

    Linking never replaces a managed name, so it needs no lock; pruning does.
    A busy lock defers pruning to the next backup instead of stalling the
    scheduler.
    """
    try:
        with backup_lock(tree, timeout_s=0):
            removed = prune(target.parent, keep=keep)
    except LockTimeoutError:
        _log.info("[backup] prune deferred while another backup owns the lock")
        removed = []
    _log_written(target, removed)


# meiva:note
# One-line DB composition for the backup log: total, the LangGraph
# checkpoint tables, and everything else.
#
# The checkpoint tables dominate DB size and dump time, so each artifact
# carries its own growth baseline. Best-effort: a failure (e.g. a fresh
# cluster missing the tables) degrades to "unavailable" and never fails the
# backup. `db_url` is the same database the dump reads.
def db_size_breakdown(db_url: str) -> str:
    try:
        with connect_url(
            db_url, autocommit=True, connect_timeout=_BREAKDOWN_CONNECT_TIMEOUT_S
        ) as conn:
            row = conn.execute(
                """
                SELECT pg_database_size(current_database()),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_blobs')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoints')), 0),
                       COALESCE(pg_total_relation_size(to_regclass('public.checkpoint_writes')), 0)
                """
            ).fetchone()
    except psycopg.Error:
        _log.warning("[backup] db size breakdown unavailable", exc_info=True)
        return "unavailable"
    assert row is not None  # noqa: S101 — aggregate over fixed tables always returns one row
    total, blobs, checkpoints, writes = (int(v) for v in row)
    return size_breakdown(total, blobs, checkpoints, writes)


def _log_written(target: Path, removed: list[Path]) -> None:
    _log.info(
        "[backup] wrote %s (%.1f MiB), pruned %d",
        target,
        target.stat().st_size / 2**20,
        len(removed),
    )


# meiva:note
# How each pipeline stage is bounded and narrated.
@dataclass(frozen=True)
class _Bound:
    timeout_s: float
    progress: _ProgressSink | None
    interval_s: float


# meiva:note
# Write one managed dump while `backup_lock` is held.
#
# Pipeline: `pg_dump --format=custom --compress=zstd:3` (the custom archive
# compresses in-dump; there is no separate gzip stage), then AES-CBC
# encryption. `bound.timeout_s` bounds every subprocess; the caller owns the lock.
#
# `bound.progress` narrates the two stages that may run for minutes without writing
# anything: `pg_dump` and the encryption pass (see `run_with_progress`).
def run_locked_backup(
    now: datetime,
    *,
    tree: BackupTree,
    db_url: str,
    directory: Path,
    bound: _Bound,
) -> Path:
    if now.tzinfo is None:
        raise ValueError(f"backup needs a TZ-aware datetime, got naive {now!r}")
    directory = ensure_private_dir(directory)
    db_conninfo, password = passwordless_conninfo(db_url)
    dbname = cast(str, conninfo_to_dict(db_url)["dbname"])
    _log.info("[backup] db composition: %s", db_size_breakdown(db_url))
    files = target_files(directory, dbname, now, {entry.name for entry in directory.iterdir()})
    files.dump_partial.touch(mode=0o600, exist_ok=False)
    try:
        _dump_and_encrypt(files, db_conninfo, password, tree, bound)
        files.encrypted_partial.rename(files.target)
    finally:
        # Every tool below is a reaped direct child once control returns here,
        # so its plaintext output has no live writer and never outlives the run.
        for partial in (files.dump_partial, files.encrypted_partial):
            partial.unlink(missing_ok=True)
    ensure_private_file(files.target)
    return files.target


def _dump_and_encrypt(
    files: DumpFiles, db_conninfo: str, password: str, tree: BackupTree, bound: _Bound
) -> None:
    files.dump_partial.chmod(0o600)
    # The scheduler owns this subprocess in its own process, so its bound
    # cannot delay watchdog supervision. Expiry kills the child and
    # TimeoutExpired lets the scheduler schedule its retry.
    proc = run_with_progress(
        pg_dump_argv(str(pg_tool("pg_dump")), files.dump_partial, db_conninfo),
        timeout_s=bound.timeout_s,
        label="pg_dump",
        progress=bound.progress,
        env=pg_dump_env(password),
        size_path=files.dump_partial,
        interval_s=bound.interval_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump exited {proc.returncode}")

    files.encrypted_partial.touch(mode=0o600, exist_ok=False)
    files.encrypted_partial.chmod(0o600)
    key_file = backup_passphrase.write_key_file(files.dump_partial.parent, tree.home)
    try:
        proc = run_with_progress(
            encrypt_argv(key_file, files.dump_partial, files.encrypted_partial),
            timeout_s=bound.timeout_s,
            label="backup encryption",
            progress=bound.progress,
            env=inherited_process_env(),
            size_path=files.encrypted_partial,
            interval_s=bound.interval_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"backup encryption exited {proc.returncode}")
    finally:
        with suppress(OSError):
            key_file.unlink(missing_ok=True)


def run_offsite_publisher(argv: list[str] | None = None) -> int:
    """Run the detached, best-effort off-site backup publisher."""
    # Standalone runs leave the module's INFO records to lastResort (WARNING+
    # only): without this the store-verified publish ACK never appears, so a
    # successful upload reads as a silent death (2026-09-16 misdiagnosis).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="python -m services.backup.dump")
    parser.add_argument("--publish-offsite", type=Path, metavar="ARTIFACT")
    parser.add_argument(
        "--offsite-root",
        default=REMOTE_ROOT,
        metavar="PREFIX",
        help=f"object-name prefix of the off-site namespace (default {REMOTE_ROOT})",
    )
    args = parser.parse_args(argv)
    if args.publish_offsite is None:
        parser.error("--publish-offsite is required")
    artifact = args.publish_offsite
    if not artifact.is_absolute():
        parser.error("--publish-offsite ARTIFACT must be an absolute path")
    if not args.offsite_root or args.offsite_root != args.offsite_root.strip("/"):
        parser.error("--offsite-root must be a non-empty prefix without surrounding slashes")
    from base.config import ConfigBoot
    from services.backup.artifact import offsite  # the SDK loads only on this entry

    config = ConfigBoot()
    config.boot()
    services = config.view.services
    target = offsite.configured_target(
        services.backup_offsite_endpoint,
        services.backup_offsite_bucket,
        services.backup_offsite_credentials_file,
    )
    offsite.publish(artifact, target, root=args.offsite_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_offsite_publisher())
