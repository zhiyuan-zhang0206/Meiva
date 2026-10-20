from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from base.log import logger
from services.backup.walg import probe
from services.backup.walg.archive import ExpectedArchive, expected_archive

# meiva:note
# `archive_mode` is postmaster-context: a retained postmaster keeps the launch
# arguments it was born with, so archiving can be configured and still not run
# until the next stop + start. The daily probe samples a full period later —
# 2026-10-17 lost 1921 WAL segments in a 14-minute maintenance window this way.
# This check runs once per start (one mismatch, one line), logs at error level
# with the machine and both value sets, and never raises: an unreadable server
# warns and the start continues (fail loud, not fail closed).


@dataclass(frozen=True)
class ActualArchive:
    mode: str
    timeout_s: int
    command: str


def read_actual(conn: psycopg.Connection[Any]) -> ActualArchive:
    row = conn.execute(
        "SELECT current_setting('archive_mode'), "
        "(SELECT setting::int FROM pg_settings WHERE name = 'archive_timeout'), "
        "current_setting('archive_command')"
    ).fetchone()
    if row is None:
        raise RuntimeError("Postgres returned no archive settings")
    return ActualArchive(mode=str(row[0]), timeout_s=int(row[1]), command=str(row[2]))


# meiva:note
# `archive_mode` is compared literally, so a standby running `always` would report a
# mismatch on every start. No Meiva home uses `always` today; a standby profile
# would have to teach this comparison about it first.
def differs(actual: ActualArchive, expected: ExpectedArchive) -> bool:
    return (actual.mode, actual.timeout_s, actual.command) != (
        expected.mode,
        expected.timeout_s,
        expected.command,
    )


def mismatch_message(machine: str, actual: ActualArchive, expected: ExpectedArchive) -> str:
    return (
        f"WAL archiving: {machine} started Postgres without the configured archive settings — "
        f"archive_mode={actual.mode} (expected {expected.mode}), "
        f"archive_timeout={actual.timeout_s}s (expected {expected.timeout_s}s), "
        f"archive_command={actual.command!r} (expected {expected.command!r}). "
        "Stop this unit with `meiva stop`, then start it again with `meiva start` and its explicit service selection."
    )


def assert_configured_archive(
    *,
    machine: str,
    path_reader: Callable[[], Path | None],
    connect: Callable[[], AbstractContextManager[psycopg.Connection[Any]]] = probe.admin_connection,
) -> str | None:
    try:
        expected = expected_archive(path_reader=path_reader)
        if expected is None:
            return None
        with connect() as conn:
            actual = read_actual(conn)
    except Exception as exc:
        logger.warning(
            f"[walg] {machine}: archive settings unreadable at startup: {type(exc).__name__}: {exc}"
        )
        return None
    if not differs(actual, expected):
        return None
    message = mismatch_message(machine, actual, expected)
    logger.error(message)
    sys.stderr.write(message + "\n")
    return message
