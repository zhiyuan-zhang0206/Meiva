"""The recovery drill: the arithmetic and the schedule, then the whole drill on a real Postgres.

The integration tests run a source Postgres that archives into the fake wal-g's store, take
a base backup of it (`pg_basebackup` standing in for `backup-push`), keep writing, and drill:
the backup is fetched back through the fake, recovered to the start of the newest archived
segment by a scratch postmaster, and read through the production checkpoint reader.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.clock import SYSTEM_TIMERS
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from services.backup.walg import drill
from services.backup.walg.backups import Backup, parse_backups
from services.backup.walg.drill import SourceFacts
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.probe import DRILL_PERIOD, TICK_PERIOD
from services.backup.walg.state import DrillRecord
from services.backup.walg.tests.support import (
    PgInstance,
    Sandbox,
    archive_current_segment,
    archiving_postgres,
    make_sandbox,
    refused_target,
    take_basebackup,
)

T0 = datetime(2026, 10, 2, 6, 25, 0, tzinfo=UTC)


# meiva:note
# The instant the drill writes into its record: the tests read the real wall clock here, as the
# tick hands the drill its own `_now`, and no case asserts a fixed `finished_at`.
def _wall_now() -> datetime:
    return datetime.now(UTC)


SEGMENT = 16 * 1024 * 1024


def _database_factory() -> Callable[[str], Database]:
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="backup-test", end_process=hard_exit)

    def for_url(url: str) -> Database:
        return Database(replace(db_config_from_settings(), db_url=url), gate=gate)

    return for_url


def _backup(**overrides: Any) -> Backup:
    base = Backup(
        name="base_000000010000000000000003",
        start_time=T0,
        uncompressed_bytes=1000,
        compressed_bytes=100,
        start_lsn=0x3000028,
        finish_lsn=0x3000138,
    )
    return replace(base, **overrides)


def _facts(**overrides: Any) -> SourceFacts:
    base = SourceFacts(
        last_archived_wal="000000010000000000000008",
        segment_bytes=SEGMENT,
        max_wal_size_bytes=1 << 30,
        data_bytes=5 * SEGMENT,
    )
    return replace(base, **overrides)


# ── arithmetic ───────────────────────────────────────────────────────────────


def test_lsns_are_written_as_postgres_writes_them() -> None:
    assert drill.format_lsn(0x2000028) == "0/2000028"
    assert drill.format_lsn(0xA3000000) == "0/A3000000"
    assert drill.format_lsn((7 << 32) + 0x1A) == "7/1A"


@pytest.mark.parametrize(
    ("wal_file", "expected"),
    [
        ("000000010000000000000003", 3 * SEGMENT),
        ("0000000100000001000000FF", (1 * 256 + 255) * SEGMENT),
        ("0000000200000000000000A0", 0xA0 * SEGMENT),
        ("000000010000000000000003.partial", 3 * SEGMENT),
        ("000000010000000000000003.00000028.backup", 3 * SEGMENT),
        ("00000002.history", None),
        ("not-a-wal-file", None),
    ],
)
def test_a_segment_starts_at_its_number_times_the_segment_size(
    wal_file: str, expected: int | None
) -> None:
    assert drill.segment_start_lsn(wal_file, SEGMENT) == expected


def test_a_larger_segment_size_scales_the_start() -> None:
    assert (
        drill.segment_start_lsn("000000010000000000000003", 64 * 1024 * 1024)
        == 3 * 64 * 1024 * 1024
    )
    assert drill.segment_start_lsn("000000010000000100000000", 64 * 1024 * 1024) == 1 << 32


def test_the_target_is_the_start_of_the_newest_archived_segment() -> None:
    assert drill.target_lsn(_backup(), _facts()) == 8 * SEGMENT


@pytest.mark.parametrize(
    "facts",
    [
        _facts(last_archived_wal="000000010000000000000003"),  # the backup's own segment
        _facts(last_archived_wal="00000002.history"),
        _facts(last_archived_wal=None),
    ],
    ids=["nothing-newer-than-the-backup", "history-file", "nothing-archived"],
)
def test_without_a_newer_archived_segment_there_is_no_target(facts: SourceFacts) -> None:
    assert drill.target_lsn(_backup(), facts) is None


def test_the_scratch_space_is_the_cluster_it_rebuilds_with_headroom() -> None:
    facts = _facts(data_bytes=100 << 20)
    assert drill.required_bytes(_backup(), facts) == (
        math.ceil((100 << 20) * drill.SPACE_FACTOR) + (1 << 30)
    )


# meiva:note
# The regression that cost 2026-10-30: a delta backup records its own bytes (2.0 GiB), and the
# drill that trusted that figure asked for 26 GiB, wrote 44 GiB and left the host at 99%.
def test_a_delta_backups_own_size_never_stands_in_for_the_cluster() -> None:
    delta = _backup(uncompressed_bytes=2 << 30)
    facts = _facts(data_bytes=42 << 30)

    assert drill.required_bytes(delta, facts) == math.ceil((42 << 30) * drill.SPACE_FACTOR) + (
        1 << 30
    )


def test_a_full_backup_larger_than_the_source_is_not_under_read() -> None:
    full = _backup(uncompressed_bytes=60 << 30)

    assert drill.required_bytes(full, _facts(data_bytes=42 << 30)) == math.ceil(
        (60 << 30) * drill.SPACE_FACTOR
    ) + (1 << 30)


# meiva:note
# The four boundaries of the rule: room to spare, exactly enough (the floor is kept, not
# exceeded), one byte short of the floor, and nothing free at all.
@pytest.mark.parametrize(
    ("required", "floor", "available", "shortfall"),
    [(10, 5, 100, 0), (10, 5, 15, 0), (10, 5, 14, 1), (10, 5, 0, 15)],
)
def test_the_shortfall_is_what_the_host_cannot_afford(
    required: int, floor: int, available: int, shortfall: int
) -> None:
    assert drill.room_shortfall(required, floor, available) == shortfall


def test_the_sweep_removes_every_leftover_drill_directory(tmp_path: Path) -> None:
    first = tmp_path / "meiva-walg-drill-first"
    second = tmp_path / "meiva-walg-drill-second"
    other = tmp_path / "unrelated"
    for path in (first, second, other):
        path.mkdir()
    (second / "data").mkdir()
    _ = (second / "data" / "PG_VERSION").write_text("17")

    sweep = drill.sweep_stale_scratch(tmp_path)

    assert [path.name for path in sweep.removed] == [first.name, second.name]
    assert sweep.failed == ()
    assert sweep.freed_bytes == len("17")
    assert not first.exists()
    assert not second.exists()
    assert other.exists()
    assert sweep.summary() == (
        f"scratch: removed 2 leftover drill directories (0 MiB) on {tmp_path}"
    )


# meiva:note
# A root with no leftover reports nothing at all: a line saying "removed 0" on every tick would be
# noise, and the tick's output is read line by line against what its steps did.
def test_a_sweep_that_finds_no_leftover_has_nothing_to_say(tmp_path: Path) -> None:
    (tmp_path / "unrelated").mkdir()

    sweep = drill.sweep_stale_scratch(tmp_path)

    assert sweep == drill.ScratchSweep(root=tmp_path)
    assert sweep.summary() is None


# meiva:note
# A root that is not there holds no leftover. `default_root` names the root here on purpose: with
# it the sweep reads the roots the caller chose, not this machine's /dev/shm and /var/tmp.
def test_sweep_scratch_roots_reads_the_roots_it_is_given(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    (present / "meiva-walg-drill-gone").mkdir()

    sweeps = drill.sweep_scratch_roots(None, environ={}, default_root=present)

    assert [sweep.root for sweep in sweeps] == [present]
    assert len(sweeps[0].removed) == 1
    assert drill.sweep_scratch_roots(None, environ={}, default_root=tmp_path / "absent") == ()


# meiva:note
# The gate's four numbers in one line. Pinned verbatim because the lesson was that a passing drill
# said nothing: an operator reading this line must be able to see the terms, not just "ok".
def test_the_space_account_names_every_number_the_gate_decided_on() -> None:
    assert drill.space_account(55 << 20, 42 << 20, 1 << 20, 24 << 30, 183 << 30) == (
        "space: need 55 MiB (data 42 MiB x 1.2 + wal 1 MiB), keep 24.0 GiB, have 183.0 GiB -> ok"
    )


# ── the schedule ─────────────────────────────────────────────────────────────


def _record(**overrides: Any) -> DrillRecord:
    base = DrillRecord(
        finished_at=T0,
        ok=True,
        backup="base_1",
        target_lsn=None,
        seconds=1.0,
        detail="",
        last_ok_at=T0,
    )
    return replace(base, **overrides)


def test_a_drill_is_due_when_a_backup_exists_and_none_ever_succeeded() -> None:
    assert drill.drill_due(None, [_backup()], T0)
    assert drill.drill_due(_record(ok=False, last_ok_at=None), [_backup()], T0)


def test_no_drill_is_due_without_a_backup() -> None:
    assert not drill.drill_due(None, [], T0)


def test_the_drill_falls_due_one_tick_period_before_a_full_period() -> None:
    due_at = DRILL_PERIOD - TICK_PERIOD
    assert not drill.drill_due(_record(), [_backup()], T0 + due_at - timedelta(minutes=1))
    assert drill.drill_due(_record(), [_backup()], T0 + due_at)


def test_a_failed_drill_does_not_postpone_the_next_one_but_a_success_does() -> None:
    failed_recently = _record(ok=False, finished_at=T0, last_ok_at=T0 - timedelta(days=7))
    assert drill.drill_due(failed_recently, [_backup()], T0 + timedelta(hours=1))
    assert not drill.drill_due(_record(), [_backup()], T0 + timedelta(days=1))


# ── a failure is a record, never an exception ────────────────────────────────


def _run(
    target: PgTarget, previous: DrillRecord | None, sandbox: Sandbox, tmp_path: Path
) -> DrillRecord:
    return drill.run_drill(
        target,
        _backup(),
        previous,
        lambda _line: None,
        path=sandbox.path,
        timers=SYSTEM_TIMERS,
        now=_wall_now,
        database_for_url=_database_factory(),
        throwaway=ThrowawayConfig(
            tmp_path, home=sandbox.home, environ=os.environ, timers=SYSTEM_TIMERS
        ),
        home=sandbox.home,
        environ=sandbox.environ,
    )


# meiva:note
# The source Postgres is not there, so the facts cannot be read: a failure, not an exception.
def test_an_expected_failure_is_recorded_with_its_message_and_keeps_the_last_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    previous = _record(last_ok_at=T0 - timedelta(days=3))
    before = datetime.now(UTC)

    record = _run(refused_target(), previous, sandbox, tmp_path)

    assert (record.ok, record.backup) == (False, _backup().name)
    assert record.detail and record.detail.startswith("connection")
    assert record.last_ok_at == previous.last_ok_at
    assert before <= record.finished_at <= datetime.now(UTC)


def test_an_unexpected_failure_names_its_type() -> None:
    assert drill.failure_text(KeyError("boom")) == "KeyError: 'boom'"
    assert drill.failure_text(ValueError("surprise")) == "ValueError: surprise"


def test_an_expected_failure_is_recorded_in_its_own_words() -> None:
    assert drill.failure_text(RuntimeError("no archiver state")) == "no archiver state"


def test_a_long_failure_message_is_cut_for_the_state_file() -> None:
    record = drill.drill_record(
        _backup(),
        None,
        reached=None,
        outcome=(False, "x" * 5000),
        seconds=1.26,
        finished=T0,
    )

    assert len(record.detail) == drill.DETAIL_CHARS
    assert (record.ok, record.seconds, record.last_ok_at, record.target_lsn) == (
        False,
        1.3,
        None,
        None,
    )


def test_a_success_becomes_the_last_success_and_a_failure_keeps_the_previous_one() -> None:
    previous = _record(last_ok_at=T0 - timedelta(days=3))

    ok = drill.drill_record(
        _backup(), previous, reached=0x3000028, outcome=(True, "fine"), seconds=2.0, finished=T0
    )
    failed = drill.drill_record(
        _backup(), previous, reached=None, outcome=(False, "bad"), seconds=2.0, finished=T0
    )

    assert (ok.last_ok_at, ok.target_lsn) == (T0, "0/3000028")
    assert failed.last_ok_at == previous.last_ok_at


# ── the whole drill, on a real Postgres ──────────────────────────────────────


def _lsn_value(text: str) -> int:
    high, low = text.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def _seed_conversation(url: str, text: str) -> int:
    """One agent row and one checkpoint holding a conversation: what the drill reads back."""
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = datetime.now(UTC).isoformat()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content=text)]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(url) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )
    return agent_id


class Source:
    """A source Postgres that archives, has been backed up once and written to since."""

    def __init__(self, sandbox: Sandbox, pg: PgInstance, *, conversations: bool = True) -> None:
        self.sandbox = sandbox
        self.pg = pg
        self.url = pg.target.admin_url
        self.target = pg.target
        conn = pg.connect()
        conn.execute("CREATE TABLE agents (id serial PRIMARY KEY)")
        # every pooled session of the production reader reads the code-version gate's row
        conn.execute("CREATE TABLE deployment_state (id int PRIMARY KEY, min_code_version bigint)")
        conn.execute("INSERT INTO deployment_state VALUES (1, 0)")
        with PostgresSaver.from_conn_string(self.url) as saver:
            saver.setup()
        self.agent_before_backup = (
            _seed_conversation(self.url, "before the backup") if conversations else 0
        )
        archive_current_segment(conn, sandbox)
        name = take_basebackup(sandbox, pg, "base_000000010000000000000001")
        label = (sandbox.store_dir / "basebackups" / name / "backup_label").read_text()
        start = re.search(r"START WAL LOCATION: ([0-9A-F]+/[0-9A-F]+)", label)
        assert start is not None
        finish = conn.execute("SELECT pg_current_wal_insert_lsn()").fetchone()
        assert finish is not None
        self.agent_after_backup = (
            _seed_conversation(self.url, "written after the backup") if conversations else 0
        )
        self.after_backup_segment = archive_current_segment(conn, sandbox)
        # the newest archived segment: its start is the target, so it holds nothing needed
        self.last_segment = archive_current_segment(conn, sandbox)
        conn.close()
        sandbox.put(
            "backups.json",
            json.dumps(
                [
                    {
                        "backup_name": name,
                        "start_time": "2026-10-02T06:00:00Z",
                        "uncompressed_size": 40_000_000,
                        "compressed_size": 8_000_000,
                        "start_lsn": _lsn_value(start[1]),
                        "finish_lsn": _lsn_value(str(finish[0])),
                    }
                ]
            ),
        )

    def newest_backup(self) -> Backup:
        return parse_backups((self.sandbox.store_dir / "backups.json").read_text())[-1]


@pytest.fixture
def scratch_base(tmp_path: Path) -> Path:
    base = tmp_path / "scratch-base"
    base.mkdir()

    return base


def _run_drill(
    source: Source,
    sandbox: Sandbox,
    previous: DrillRecord | None = None,
    *,
    scratch_base: Path,
    socket_root: Path | None = None,
    host_floor_bytes: int | None = None,
) -> tuple[DrillRecord, list[str]]:
    lines: list[str] = []
    # meiva:note
    # The scratch base here is a temporary directory on the test host, not a production
    # filesystem: the host floor defaults to zero so a runner with a small disk still runs the
    # drill. The floor and the disk reading are exercised by their own cases below.
    space: dict[str, Any] = {
        "host_floor_bytes": 0 if host_floor_bytes is None else host_floor_bytes
    }
    record = drill.run_drill(
        source.target,
        source.newest_backup(),
        previous,
        lines.append,
        path=sandbox.path,
        timers=SYSTEM_TIMERS,
        now=_wall_now,
        database_for_url=_database_factory(),
        throwaway=ThrowawayConfig(
            scratch_base, home=sandbox.home, environ=os.environ, timers=SYSTEM_TIMERS
        ),
        home=sandbox.home,
        **({} if socket_root is None else {"socket_root": socket_root}),
        **space,
        environ=sandbox.environ,
    )
    return record, lines


def test_the_drill_restores_the_newest_backup_to_the_newest_archived_segment_and_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres(sandbox=sandbox) as pg:
        source = Source(sandbox, pg)

        record, lines = _run_drill(source, sandbox, scratch_base=scratch_base)

    assert record.ok, record.detail
    assert record.target_lsn == drill.format_lsn(
        drill.segment_start_lsn(source.last_segment, SEGMENT) or 0
    )
    # the conversation written after the backup is the newest one: reading it proves the
    # WAL behind the backup was replayed, not only the base backup restored
    assert f"conversation of agent {source.agent_after_backup} has 1 messages" in record.detail
    assert source.agent_after_backup != source.agent_before_backup
    assert record.last_ok_at == record.finished_at
    assert record.backup == source.newest_backup().name
    assert any(line.startswith("drill: restoring base_") for line in lines)
    assert any(line.startswith("space: need ") and line.endswith(" -> ok") for line in lines), lines
    assert list(scratch_base.iterdir()) == []  # the scratch copy is gone


# meiva:note
# The refusal has to happen before the copy is written, and it has to be a record: a drill that
# silently skipped is the "no run at all" false green, and the host it was protecting looks fine.
def test_a_drill_with_no_room_is_refused_before_it_writes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres(sandbox=sandbox) as pg:
        source = Source(sandbox, pg)

        record, lines = _run_drill(
            source, sandbox, scratch_base=scratch_base, host_floor_bytes=1 << 62
        )

    assert not record.ok
    assert "drill refused, nothing written" in record.detail
    assert "short by" in record.detail
    assert record.last_refused_at == record.finished_at
    assert record.last_refused_reason == record.detail
    assert record.last_ok_at is None
    assert list(scratch_base.iterdir()) == []
    assert any(line.startswith("drill: restoring base_") for line in lines) is False


def test_a_leftover_scratch_directory_is_removed_before_the_drill_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    stale = scratch_base / "meiva-walg-drill-from-a-killed-run"
    (stale / "data").mkdir(parents=True)
    (stale / "data" / "PG_VERSION").write_text("17")

    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres(sandbox=sandbox) as pg:
        source = Source(sandbox, pg)

        record, lines = _run_drill(source, sandbox, scratch_base=scratch_base)

    assert record.ok, record.detail
    assert any(
        line == f"scratch: removed 1 leftover drill directory (0 MiB) on {scratch_base}"
        for line in lines
    )
    assert list(scratch_base.iterdir()) == []


def test_a_missing_segment_after_the_backup_fails_the_drill_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    socket_root = Path(tempfile.mkdtemp(prefix="walgsock-", dir="/tmp"))
    try:
        with archiving_postgres(sandbox=sandbox) as pg:
            source = Source(sandbox, pg)
            (sandbox.store_dir / "store" / source.after_backup_segment).unlink()

            record, _ = _run_drill(
                source, sandbox, scratch_base=scratch_base, socket_root=socket_root
            )

        assert not record.ok
        assert "recovery failed" in record.detail
        assert record.last_ok_at is None
        # meiva:note
        # the target was chosen before the restore failed
        assert record.target_lsn is not None
        assert list(scratch_base.iterdir()) == []
        assert list(socket_root.iterdir()) == []
    finally:
        shutil.rmtree(socket_root, ignore_errors=True)


def test_without_anything_archived_after_the_backup_the_drill_recovers_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres(sandbox=sandbox) as pg:
        source = Source(sandbox, pg)
        listed = json.loads((sandbox.store_dir / "backups.json").read_text())
        listed[0]["finish_lsn"] = 1 << 60  # the backup "ends" after everything archived
        sandbox.put("backups.json", json.dumps(listed))

        record, _ = _run_drill(source, sandbox, scratch_base=scratch_base)

    assert record.ok, record.detail
    assert record.target_lsn is None
    assert f"conversation of agent {source.agent_after_backup}" in record.detail


def test_a_database_without_a_conversation_fails_the_content_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_base: Path, postgres_access: None
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres(sandbox=sandbox) as pg:
        source = Source(sandbox, pg, conversations=False)

        record, _ = _run_drill(source, sandbox, scratch_base=scratch_base)

    assert not record.ok
    assert "no readable agent conversation" in record.detail
    assert list(scratch_base.iterdir()) == []
