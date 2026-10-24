from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from psycopg.conninfo import conninfo_to_dict

from services.backup import dump
from services.backup.dump_lines import (
    beat_line,
    restore_failure_message,
    size_breakdown,
    started_line,
)
from services.backup.dump_plan import (
    decrypt_argv,
    encrypt_argv,
    pg_dump_argv,
    pg_dump_env,
    prune_victims,
    target_files,
)

_DIR = Path("/backups/db")


def _at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# meiva:note
# The stamp is UTC with an explicit `Z`, not cluster wall clock: the name has to
# identify one instant for prune's ordering to be a total order.
def test_the_dump_name_is_stamped_in_utc_whatever_zone_the_instant_carries() -> None:
    shanghai = datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    files = target_files(_DIR, "meiva", shanghai, frozenset())

    assert files.target == _DIR / "meiva-20260609T190000Z.dump.enc"


def test_the_two_intermediates_are_named_after_the_artifact() -> None:
    files = target_files(_DIR, "meiva", _at(2026, 8, 8, 10), frozenset())

    assert files.dump_partial == _DIR / "meiva-20260808T100000Z.dump.partial"
    assert files.encrypted_partial == _DIR / "meiva-20260808T100000Z.dump.enc.partial"


def test_a_taken_name_moves_the_stamp_one_second_later() -> None:
    taken = {"whatever-20260808T100000Z.dump.enc"}

    files = target_files(_DIR, "whatever", _at(2026, 8, 8, 10), taken)

    assert files.target.name == "whatever-20260808T100001Z.dump.enc"


def test_sixty_taken_seconds_in_a_row_are_a_fault() -> None:
    taken = {f"x-20260808T1000{second:02d}Z.dump.enc" for second in range(60)}

    with pytest.raises(RuntimeError, match="distinct backup filename"):
        target_files(_DIR, "x", _at(2026, 8, 8, 10), taken)


# meiva:note
# The recoverable dump keeps the checkpoint tables: they are the sole copy of conversation history.
def test_pg_dump_compresses_in_dump_and_excludes_no_table() -> None:
    argv = pg_dump_argv(
        "/pg/bin/pg_dump", _DIR / "x.dump.partial", "dbname=whatever host=db.example"
    )

    assert argv[0] == "/pg/bin/pg_dump"
    assert "--format=custom" in argv
    assert "--compress=zstd:3" in argv
    assert not [arg for arg in argv if arg.startswith("--exclude")]
    assert argv[argv.index("--file") + 1] == str(_DIR / "x.dump.partial")
    assert argv[argv.index("--dbname") + 1] == "dbname=whatever host=db.example"


def test_the_password_travels_only_in_the_child_environment() -> None:
    conninfo, sentinel = dump.passwordless_conninfo(
        "postgresql://backup:backup-password@db.example:5432/whatever"
    )

    assert sentinel == "backup-password"
    assert conninfo_to_dict(conninfo) == {
        "dbname": "whatever",
        "host": "db.example",
        "port": "5432",
        "user": "backup",
    }
    assert sentinel not in " ".join(pg_dump_argv("pg_dump", _DIR / "x", conninfo))
    assert pg_dump_env(sentinel) == {"PGPASSWORD": sentinel}


# meiva:note
# A shell's PGPASSWORD must never authenticate a dump against another cluster.
def test_a_dump_without_a_password_gets_an_empty_environment() -> None:
    conninfo, carried = dump.passwordless_conninfo("postgresql://meiva@127.0.0.1:5433/meiva")

    assert carried == ""
    assert pg_dump_env(carried) == {}
    assert conninfo_to_dict(conninfo)["user"] == "meiva"


def test_encryption_and_decryption_are_one_cipher_with_the_key_in_a_file() -> None:
    key, source, out = Path("/k"), Path("/in"), Path("/out")

    encrypt = encrypt_argv(key, source, out)
    decrypt = decrypt_argv(key, source, out)

    assert {"enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-kfile"}.issubset(encrypt)
    assert "-d" not in encrypt
    assert decrypt == [*encrypt[:2], "-d", *encrypt[2:]]
    assert encrypt[encrypt.index("-kfile") + 1] == "/k"


def test_retention_keeps_the_newest_and_names_the_rest_oldest_first() -> None:
    dumps = [(_at(2026, 6, day, 10), Path(f"d{day}")) for day in range(1, 11)]

    assert prune_victims(dumps, 7) == [Path("d1"), Path("d2"), Path("d3")]
    assert prune_victims(dumps[:3], 7) == []


# meiva:note
# Two dumps at 01:30 on the fold day are one hour apart; the earlier half goes first.
def test_retention_orders_the_dst_fold_by_instant_not_by_wall_time() -> None:
    # meiva:note
    # 01:30 PDT
    pdt = (datetime(2026, 11, 1, 8, 30, tzinfo=UTC), Path("pdt"))
    # meiva:note
    # 01:30 PST
    pst = (datetime(2026, 11, 1, 9, 30, tzinfo=UTC), Path("pst"))

    assert prune_victims([pst, pdt], 1) == [Path("pdt")]


def test_retention_refuses_to_keep_nothing() -> None:
    with pytest.raises(ValueError, match="at least one"):
        prune_victims([(_at(2026, 6, 1, 10), Path("d1"))], 0)


# meiva:note
# The log line reports the composition that dominates dump time.
def test_the_size_breakdown_names_total_checkpoint_and_rest() -> None:
    assert size_breakdown(4_240_000_000, 1_240_000_000, 130_000_000, 30_000_000) == (
        "db=4044MiB checkpoint=1335MiB rest=2708MiB"
    )
    assert size_breakdown(0, 0, 0, 0) == "db=0MiB checkpoint=0MiB rest=0MiB"


def test_a_stage_narrates_its_bound_and_then_its_progress() -> None:
    assert started_line("pg_dump", 120.0) == "pg_dump started (bounded at 2 min)"
    assert beat_line("pg_dump", 61.4, 3 * 2**20) == "pg_dump 61s, 3.0 MiB written"
    assert beat_line("pg_dump", 61.4, None) == "pg_dump 61s"


# meiva:note
# A server killed mid-COPY must not surface as a bare exit code (the 2026-09-14 failure).
def test_a_failed_restore_names_the_base_its_free_space_and_the_knob_that_moves_it() -> None:
    message = restore_failure_message(
        1,
        b"pg_restore: error: PQputCopyData: server closed the connection unexpectedly\n",
        Path("/scratch/base"),
        "512 MiB",
    )

    assert "pg_restore exited 1" in message
    assert "/scratch/base" in message
    assert "512 MiB free" in message
    assert "MEIVA_PG_THROWAWAY_BASE" in message
    assert "PQputCopyData: server closed the connection" in message
    assert restore_failure_message(1, b"", Path("/b"), "1 GiB").endswith("larger volume.")
