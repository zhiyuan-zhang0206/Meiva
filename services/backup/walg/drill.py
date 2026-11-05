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
# every outcome this process survives. What a killed one leaves is swept by the drill before it
# writes and by the daily tick, whichever comes first -- both hold the run lock that proves it dead.
#
# A drill never raises and never stops the backup that follows it: the outcome is a
# `DrillRecord` for the state file, whose failure the health probe reports.
# meiva:link services/backup/walg/docs/walg-restore.meiva.okf.md


from __future__ import annotations

import math
import re
import shutil
import tempfile
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from base.clock import Timers
from base.cluster.dataplane import pg_admin
from base.cluster.dataplane.pg_throwaway_base import (
    InsufficientThrowawaySpaceError,
    ThrowawayConfig,
    format_bytes,
    free_bytes,
    select_throwaway_base,
    throwaway_roots,
)
from base.db import Database, connect_url
from base.db.rows import query_one
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


@dataclass(frozen=True)
class _SourceRow:
    last_archived_wal: str | None
    segment_bytes: int
    max_wal_size_bytes: int
    data_bytes: int | None


def read_source_facts(target: PgTarget) -> SourceFacts:
    with pg_admin.connect(
        target.admin_url,
        expected_data_dir=target.data_dir,
        autocommit=True,
        connect_timeout=_CONNECT_TIMEOUT_S,
    ) as conn:
        row = query_one(
            conn,
            _SourceRow,
            "SELECT last_archived_wal, "
            "pg_size_bytes(current_setting('wal_segment_size'))::bigint AS segment_bytes, "
            "pg_size_bytes(current_setting('max_wal_size'))::bigint AS max_wal_size_bytes, "
            "(SELECT sum(pg_database_size(datname)) FROM pg_database)::bigint AS data_bytes "
            "FROM pg_stat_archiver",
        )
    if row is None:
        raise RuntimeError("Postgres returned no archiver state")
    return SourceFacts(
        last_archived_wal=row.last_archived_wal,
        segment_bytes=row.segment_bytes,
        max_wal_size_bytes=row.max_wal_size_bytes,
        data_bytes=max(row.data_bytes or 0, 0),
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
    return math.ceil(scratch_data_bytes(backup, facts) * factor) + facts.max_wal_size_bytes


# meiva:note
# The size the drill plans for, named once: the source's own contents unless the backup record is
# larger, which it is only for a full backup taken while the cluster held more. The space line
# below and `required_bytes` read the same function, so a logged figure cannot drift from the one
# the gate decided on.
def scratch_data_bytes(backup: Backup, facts: SourceFacts) -> int:
    return max(facts.data_bytes, backup.uncompressed_bytes)


# meiva:note
# The arithmetic of the refusal, kept apart from the reading of the disk so it can be checked
# without one: a drill may start only when it still leaves `floor` behind for the host.
def room_shortfall(required: int, floor: int, available: int) -> int:
    return max(required + floor - available, 0)


# meiva:note
# The line that says the space gate ran and on which numbers. The refusal below speaks only when it
# refuses, so a drill that passed left nothing an operator could read -- on 2026-11-06 the absence
# of any such line was read as "this drill has no space gate at all", and the gate had to be found
# in the source to be believed. The terms are the gate's own: what it needs, what it keeps, what it
# found.
def space_account(
    required: int,
    data_bytes: int,
    wal_bytes: int,
    floor: int,
    available: int,
    *,
    factor: float = SPACE_FACTOR,
) -> str:
    return (
        f"space: need {format_bytes(required)} (data {format_bytes(data_bytes)} x {factor:g}"
        f" + wal {format_bytes(wal_bytes)}), keep {format_bytes(floor)},"
        f" have {format_bytes(available)} -> ok"
    )


# meiva:note
# What a killed drill leaves behind. `finally` does not run when the process is signalled or the
# host reboots, so a 44 GiB copy stays on the filesystem it had already filled. Both callers -- the
# drill before it writes, and the daily tick -- hold the home's run lock, so any scratch directory
# under a throwaway root at that moment belongs to a dead drill. That lock *is* the criterion: the
# alternative, a directory age bound, would be a weaker guess at the same fact, and it would keep a
# killed drill's bytes across the next drill's space gate.
#
# One sweep's outcome, so the report and the count come from the same read: what was removed with
# the bytes it freed, and what is still there because this process may not remove it. `summary()`
# is the one line to report, or None when the root held nothing of the drill's -- a "removed 0" on
# every tick would be noise, and the tick's output is read line by line against what its steps did.
@dataclass(frozen=True)
class ScratchSweep:
    root: Path
    removed: tuple[Path, ...] = ()
    failed: tuple[Path, ...] = ()
    freed_bytes: int = 0

    def summary(self) -> str | None:
        if not self.removed and not self.failed:
            return None
        parts: list[str] = []
        if self.removed:
            count = len(self.removed)
            parts.append(
                f"removed {count} leftover drill "
                f"{'directory' if count == 1 else 'directories'} ({format_bytes(self.freed_bytes)})"
            )
        if self.failed:
            names = ", ".join(sorted(path.name for path in self.failed))
            parts.append(f"could not remove {names}")
        return f"scratch: {'; '.join(parts)} on {self.root}"


# meiva:note
# The bytes of the files under one directory, read before it is deleted: the freed figure is what
# the report is for. A file that vanished or cannot be read is skipped and the rest still count.
def _tree_bytes(root: Path) -> int:
    total = 0
    for entry in root.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


# meiva:note
# Remove every drill directory under `base`. Removing one that is somehow still in use fails here
# and is reported rather than hidden; the next sweep retries it.
def sweep_stale_scratch(base: Path) -> ScratchSweep:
    removed: list[Path] = []
    failed: list[Path] = []
    freed = 0
    for stale in sorted(base.glob(f"{_SCRATCH_PREFIX}*")):
        if not stale.is_dir():
            continue
        size = _tree_bytes(stale)
        shutil.rmtree(stale, ignore_errors=True)
        if stale.exists():
            failed.append(stale)
            continue
        removed.append(stale)
        freed += size
    return ScratchSweep(root=base, removed=tuple(removed), failed=tuple(failed), freed_bytes=freed)


# meiva:note
# Every root a drill could have written to, not only today's: which one it picks follows the free
# space, so a copy left under `/dev/shm` yesterday is still the drill's to clean when today it
# would pick `/var/tmp`.
def sweep_scratch_roots(
    override: Path | None, *, environ: Mapping[str, str], default_root: Path | None = None
) -> tuple[ScratchSweep, ...]:
    return tuple(
        sweep_stale_scratch(root)
        for root in throwaway_roots(override, environ=environ, default_root=default_root)
        if root.is_dir()
    )


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
    environ: Mapping[str, str],
    timers: Timers,
    space_factor: float = SPACE_FACTOR,
    host_floor_bytes: int = HOST_FLOOR_BYTES,
) -> str:
    """Restore `backup` to the newest archived segment and verify it; returns the summary."""
    # meiva:note
    # Before the gate, not after it: a killed drill's copy is deleted first, so the gate reads the
    # disk as the drill will find it. Swept afterwards, the leftovers counted against the room this
    # drill asked for, and a host that was short only because of them would have refused for good.
    for sweep in sweep_scratch_roots(
        throwaway.override, environ=environ, default_root=throwaway.default_root
    ):
        line = sweep.summary()
        if line is not None:
            report(line)
    required = required_bytes(backup, facts, factor=space_factor)
    try:
        base = select_throwaway_base(
            required + host_floor_bytes,
            override=throwaway.override,
            environ=throwaway.environ,
            default_root=throwaway.default_root,
        )
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
    report(
        space_account(
            required,
            scratch_data_bytes(backup, facts),
            facts.max_wal_size_bytes,
            host_floor_bytes,
            available,
            factor=space_factor,
        )
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
            timers=timers,
            socket_root=socket_root,
            home=home,
            environ=environ,
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


# meiva:note
# `timers` and `now` are the tick's clock, handed down: `timers` bounds the recovery this drill
# drives and measures how long it took, `now` stamps the record written here. `now` is a callable
# on purpose -- it is read where the record is built, so the stamp stays the *completion* instant
# (a pre-read instant handed in by the caller would date the drill from when it started). The
# drill reads neither itself, so a test drives both.
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
    environ: Mapping[str, str],
    timers: Timers,
    now: Callable[[], datetime],
    space_factor: float = SPACE_FACTOR,
    host_floor_bytes: int = HOST_FLOOR_BYTES,
) -> DrillRecord:
    """Run one drill against `backup`; every failure becomes a record with `ok=False`."""
    started = timers.monotonic()
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
                environ=environ,
                timers=timers,
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
        seconds=timers.monotonic() - started,
        finished=now(),
        refused=refused,
    )
