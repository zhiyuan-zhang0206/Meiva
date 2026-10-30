# meiva:note
# The weekly recovery drill: prove the newest backup and the WAL behind it restore, and read.
#
# Run by the daily tick, before that day's backup, once a week (and by hand through
# `meiva backup walg drill`). It restores what a lost host would need right now, the newest
# backup, with the same code as a real restore (`restore.py`), into a scratch directory,
# and checks three things:
#
# - **The chain is continuous.** The recovery target is the *start* LSN of the newest WAL
#   segment that is already archived (`pg_stat_archiver.last_archived_wal`), not its end:
#   Postgres stops at the first record at or beyond the target, and the first record after
#   a segment's end is in a segment that is not archived yet and would never be read. A
#   missing or unreadable segment anywhere between the backup and that point ends recovery
#   in Postgres' own FATAL, so a drill that promotes proves every segment in between is
#   present, decrypts and replays. Without a newer segment than the backup's end there is
#   nothing to prove, and the drill recovers to the end of the archive instead; the record
#   says so (`target_lsn` is None).
# - **The data is readable.** The promoted database goes through the same check the logical
#   restore drill uses (`verify_restored_database`): the required tables, row counts, a real
#   agent conversation read back through the production checkpoint reader, and a type census —
#   every class named by the newest checkpoint of the newest conversations is one this build
#   can load or has deliberately retired.
# - **The target was reached** (`pg_current_wal_lsn()` on the promoted instance).
#
# The scratch copy lives on `select_throwaway_base(required_bytes)`, where `required_bytes`
# is the size of the cluster that will be reconstructed -- read from the source, not from the
# backup record: a delta backup's `uncompressed_size` is the delta's own bytes (2 GiB on
# 2026-10-30) while the directory it rebuilds is the whole database (42 GiB), and the drill that
# trusted the record asked for 26 GiB, wrote 44 GiB and left the host at 13 GiB. It is removed in
# every outcome this process survives, and the next drill sweeps what a killed one left behind.
#
# A drill never raises and never stops the backup that follows it: the outcome is a
# `DrillRecord` for the state file, whose failure the health probe reports.
# meiva:link services/backup/walg/docs/walg-restore.meiva.okf.md


from __future__ import annotations

import math
import re
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from base.cluster.dataplane import pg_admin
from base.cluster.dataplane.pg_throwaway_base import (
    InsufficientThrowawaySpaceError,
    ThrowawayConfig,
    format_bytes,
    free_bytes,
    select_throwaway_base,
)
from base.db import Database, connect_url
from base.log import logger
from services.backup.walg.backups import Backup
from services.backup.walg.config import WalgConfigError
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.probe import DRILL_PERIOD, TICK_PERIOD
from services.backup.walg.restore import (
    SOCKET_ROOT,
    RecoveryTarget,
    RestoreError,
    restored_instance,
)
from services.backup.walg.runner import WalgCommandError
from services.backup.walg.state import DrillRecord

Report = Callable[[str], None]

_CONNECT_TIMEOUT_S = 5
DETAIL_CHARS = 1000

_SCRATCH_PREFIX = "meiva-walg-drill-"

# meiva:note
# The two numbers that decide whether a drill may start at all. The factor covers catalogs and
# filesystem overhead over the summed database sizes (2026-10-30: 43 GiB on disk for 42.5 GiB
# summed) and the drift between reading the source and writing the copy. The floor is what the
# host must still have when the copy exists: production shares this filesystem, and a drill that
# took the last 13 GiB put it at 99% -- a checkpoint burst away from stalling the cluster.
SPACE_FACTOR = 1.2
HOST_FLOOR_BYTES = 24 << 30

# `<timeline><log><segment>` in hex, optionally followed by `.partial` or `.<offset>.backup`.
_SEGMENT_FILE = re.compile(r"[0-9A-F]{8}([0-9A-F]{8})([0-9A-F]{8})(?:\..+)?")
_LSN_SPLIT = 1 << 32


# meiva:note
# A refusal is not a broken backup: the drill never ran, so nothing about the chain was proven
# either way. Its own type keeps "we last refused at T" apart from "the last drill failed", which
# is what an operator reading the state file needs to tell a full disk from a bad backup.
class DrillRefusedError(RestoreError):
    pass


_EXPECTED_FAILURES = (
    RestoreError,
    WalgCommandError,
    WalgConfigError,
    psycopg.Error,
    OSError,
    RuntimeError,  # insufficient scratch space, a failed content check
)


def drill_due(previous: DrillRecord | None, backups: list[Backup], now: datetime) -> bool:
    """Whether a drill should run now: a backup exists and none succeeded for a period.

    The test is one tick period short of the full period, because ticks are a period
    apart: with the full period, a drill that finished a few minutes into its tick would
    be a few minutes short at the next one and wait a whole extra day.
    """
    if not backups:
        return False
    last_ok = None if previous is None else previous.last_ok_at
    return last_ok is None or now - last_ok >= DRILL_PERIOD - TICK_PERIOD


def format_lsn(value: int) -> str:
    """`33554472` as Postgres writes it: `0/2000028`."""
    return f"{value // _LSN_SPLIT:X}/{value % _LSN_SPLIT:X}"


def segment_start_lsn(wal_file: str, segment_bytes: int) -> int | None:
    """The LSN at which the WAL segment named `wal_file` starts; None for a file that is no segment.

    Timeline history files have no segment number. A `.partial` segment or a backup label
    file belongs to the segment it names.
    """
    match = _SEGMENT_FILE.fullmatch(wal_file)
    if match is None:
        return None
    log, segment = int(match[1], 16), int(match[2], 16)
    return (log * (_LSN_SPLIT // segment_bytes) + segment) * segment_bytes


@dataclass(frozen=True)
class SourceFacts:
    """What the live database says about its archive, read once before the restore."""

    last_archived_wal: str | None
    segment_bytes: int
    max_wal_size_bytes: int
    data_bytes: int


def read_source_facts(target: PgTarget) -> SourceFacts:
    with pg_admin.connect(
        target.admin_url,
        expected_data_dir=target.data_dir,
        autocommit=True,
        connect_timeout=_CONNECT_TIMEOUT_S,
    ) as conn:
        row = conn.execute(
            "SELECT last_archived_wal, pg_size_bytes(current_setting('wal_segment_size')), "
            "pg_size_bytes(current_setting('max_wal_size')), "
            "(SELECT sum(pg_database_size(datname)) FROM pg_database) FROM pg_stat_archiver"
        ).fetchone()
    if row is None:
        raise RuntimeError("Postgres returned no archiver state")
    return SourceFacts(
        last_archived_wal=None if row[0] is None else str(row[0]),
        segment_bytes=int(row[1]),
        max_wal_size_bytes=int(row[2]),
        data_bytes=max(int(row[3] or 0), 0),
    )


# meiva:note
# Room the scratch copy needs: the cluster it rebuilds, with headroom, plus one `max_wal_size`.
# The data size comes from the source, never from the backup record alone: a delta backup records
# the delta's own bytes (2.0 GiB on 2026-10-30) while the directory it rebuilds is the whole
# cluster (42.5 GiB). The `max()` keeps a full backup that is larger than the source's current
# contents from under-reading. The WAL replay is not added: Postgres removes restored segments as
# it passes them (measured 2026-10-30, 1.2-1.4 GiB in `pg_wal` while 24 GiB of WAL was replayed),
# so `max_wal_size` is the bound.
def required_bytes(backup: Backup, facts: SourceFacts, *, factor: float = SPACE_FACTOR) -> int:
    data = max(facts.data_bytes, backup.uncompressed_bytes)
    return math.ceil(data * factor) + facts.max_wal_size_bytes


# meiva:note
# The arithmetic of the refusal, kept apart from the reading of the disk so it can be checked
# without one: a drill may start only when it still leaves `floor` behind for the host.
def room_shortfall(required: int, floor: int, available: int) -> int:
    return max(required + floor - available, 0)


# meiva:note
# What a killed drill leaves behind. `finally` does not run when the process is signalled or the
# host reboots, so a 44 GiB copy stays on the filesystem it had already filled. The next drill
# removes it before writing its own: the run lock admits one drill at a time, so any scratch
# directory under the base at this point belongs to a dead one.
def sweep_stale_scratch(base: Path) -> int:
    removed = 0
    for stale in sorted(base.glob(f"{_SCRATCH_PREFIX}*")):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
    return removed


def target_lsn(backup: Backup, facts: SourceFacts) -> int | None:
    """The start of the newest archived segment, if it is newer than the backup's end."""
    if facts.last_archived_wal is None:
        return None
    start = segment_start_lsn(facts.last_archived_wal, facts.segment_bytes)
    return start if start is not None and start > backup.finish_lsn else None


def _admin_user(target: PgTarget) -> str:
    user = urlsplit(target.admin_url).username
    if not user:
        raise RuntimeError("the admin URL names no user")
    return user


def _verify(
    instance_url: str, reached: int | None, *, database_for_url: Callable[[str], Database]
) -> str:
    """Content check and target check on the promoted scratch instance; returns the summary."""
    from services.backup.restore_drill import verify_restored_database

    report = verify_restored_database(instance_url, database_for_url=database_for_url)
    if reached is not None:
        with connect_url(instance_url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
            row = conn.execute(
                "SELECT pg_current_wal_lsn() >= %s::pg_lsn", (format_lsn(reached),)
            ).fetchone()
        if row is None or not row[0]:
            raise RestoreError("the recovered instance was promoted before the recovery target")
    return (
        f"{report.agents} agents, {report.checkpoints} checkpoints, "
        f"conversation of agent {report.sample_agent_id} has {report.sample_message_count}"
        f" messages; {report.census.summary()}"
    )


def _restore_and_verify(
    target: PgTarget,
    backup: Backup,
    facts: SourceFacts,
    reached: int | None,
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    path: Path | None,
    database_for_url: Callable[[str], Database],
    socket_root: Path,
    home: Path,
    space_factor: float = SPACE_FACTOR,
    host_floor_bytes: int = HOST_FLOOR_BYTES,
) -> str:
    """Restore `backup` to the newest archived segment and verify it; returns the summary."""
    required = required_bytes(backup, facts, factor=space_factor)
    try:
        base = select_throwaway_base(required + host_floor_bytes, override=throwaway.override)
    except InsufficientThrowawaySpaceError as exc:
        raise DrillRefusedError(f"drill refused, nothing written: {exc}") from exc
    available = free_bytes(base)
    shortfall = room_shortfall(required, host_floor_bytes, available)
    if shortfall:
        raise DrillRefusedError(
            f"drill refused, nothing written: {base} holds {format_bytes(available)}, and the"
            f" drill needs {format_bytes(required)} for the scratch copy plus"
            f" {format_bytes(host_floor_bytes)} kept free for the host — short by"
            f" {format_bytes(shortfall)}"
        )
    leftover = sweep_stale_scratch(base)
    if leftover:
        report(
            f"scratch: removed {leftover} leftover drill "
            f"{'directory' if leftover == 1 else 'directories'} on {base}"
        )
    report(
        f"drill: restoring {backup.name} on {base} to "
        f"{'LSN ' + format_lsn(reached) if reached is not None else 'the end of the archive'}"
    )
    scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=base))
    try:
        with restored_instance(
            scratch / "data",
            backup=backup.name,
            target=RecoveryTarget(lsn=None if reached is None else format_lsn(reached)),
            report=report,
            user=_admin_user(target),
            keep_data=False,
            path=path,
            socket_root=socket_root,
            home=home,
        ) as instance:
            summary = _verify(
                instance.url(target.database), reached, database_for_url=database_for_url
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        report(f"scratch: removed {scratch.name}")
    return summary


# meiva:note
# The record's detail for a failure: an expected one says what went wrong in its own
# words; anything else is a bug and is recorded with its type as well.
def failure_text(exc: BaseException) -> str:
    if isinstance(exc, _EXPECTED_FAILURES):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


# meiva:note
# The state-file record of one drill: `outcome` is (ok, detail); a failure keeps the last
# success of `previous`.
def drill_record(
    backup: Backup,
    previous: DrillRecord | None,
    *,
    reached: int | None,
    outcome: tuple[bool, str],
    seconds: float,
    finished: datetime,
    refused: str | None = None,
) -> DrillRecord:
    ok, detail = outcome
    return DrillRecord(
        finished_at=finished,
        ok=ok,
        backup=backup.name,
        target_lsn=None if reached is None else format_lsn(reached),
        seconds=round(seconds, 1),
        detail=detail[:DETAIL_CHARS],
        last_ok_at=finished if ok else (None if previous is None else previous.last_ok_at),
        last_refused_at=(
            finished
            if refused is not None
            else (None if previous is None else previous.last_refused_at)
        ),
        last_refused_reason=(
            refused
            if refused is not None
            else (None if previous is None else previous.last_refused_reason)
        ),
    )


def run_drill(
    target: PgTarget,
    backup: Backup,
    previous: DrillRecord | None,
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    path: Path | None,
    database_for_url: Callable[[str], Database],
    socket_root: Path = SOCKET_ROOT,
    home: Path,
    space_factor: float = SPACE_FACTOR,
    host_floor_bytes: int = HOST_FLOOR_BYTES,
) -> DrillRecord:
    """Run one drill against `backup`; every failure becomes a record with `ok=False`."""
    started = time.monotonic()
    reached: int | None = None
    refused: str | None = None
    try:
        facts = read_source_facts(target)
        reached = target_lsn(backup, facts)
        outcome = (
            True,
            _restore_and_verify(
                target,
                backup,
                facts,
                reached,
                report,
                throwaway=throwaway,
                path=path,
                database_for_url=database_for_url,
                socket_root=socket_root,
                home=home,
                space_factor=space_factor,
                host_floor_bytes=host_floor_bytes,
            ),
        )
    except DrillRefusedError as exc:
        refused = str(exc)
        outcome = (False, refused)
    except _EXPECTED_FAILURES as exc:
        outcome = (False, failure_text(exc))
    except Exception as exc:  # any other failure is still the drill's, never a crash of the tick
        logger.error(f"[walg] recovery drill failed: {traceback.format_exc()}")
        outcome = (False, failure_text(exc))
    return drill_record(
        backup,
        previous,
        reached=reached,
        outcome=outcome,
        seconds=time.monotonic() - started,
        finished=datetime.now(UTC),
        refused=refused,
    )
