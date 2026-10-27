from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import psycopg

from base.log import logger
from services.backup.walg.archive import expected_for
from services.backup.walg.archive_plan import ActualArchive, differs, mismatch_message

# meiva:note
# `archive_mode` is postmaster-context: a retained postmaster keeps the launch
# arguments it was born with, so archiving can be configured and still not run
# until the next stop + start. The daily probe samples a full period later —
# 2026-10-17 lost 1921 WAL segments in a 14-minute maintenance window this way.
# This check runs once per start (one mismatch, one line), logs at error level
# with the machine and both value sets, and never raises: an unreadable server
# warns and the start continues (fail loud, not fail closed).


def read_actual(conn: psycopg.Connection[Any]) -> ActualArchive:
    row = conn.execute(
        "SELECT current_setting('archive_mode'), "
        "(SELECT setting::int FROM pg_settings WHERE name = 'archive_timeout'), "
        "current_setting('archive_command')"
    ).fetchone()
    if row is None:
        raise RuntimeError("Postgres returned no archive settings")
    return ActualArchive(mode=str(row[0]), timeout_s=int(row[1]), command=str(row[2]))


def assert_configured_archive(
    *,
    machine: str,
    path: Path | None,
    admin: AbstractContextManager[psycopg.Connection[Any]],
    home: Path,
) -> str | None:
    try:
        expected = expected_for(path, home=home)
        if expected is None:
            return None
        with admin as conn:
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
