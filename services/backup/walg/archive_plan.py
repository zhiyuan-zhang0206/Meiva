# meiva:note
# What Postgres is told about WAL archiving and what it is expected to report, as values in and
# values out.
#
# `archive.py` resolves the configured file and the pinned binary; this module owns the
# settings themselves: the three launch arguments, the archive command text with its `%`
# escaping, and the comparison between what a running Postgres reports and what the code
# launches it with.

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ARCHIVE_TIMEOUT_S",
    "RPO_OBJECTIVE_S",
    "ActualArchive",
    "ExpectedArchive",
    "archive_args",
    "differs",
    "expected_settings",
    "mismatch_message",
    "postgres_command",
]

# meiva:note
# Force a WAL segment switch at least this often. Design ruling (RPO <= 5 minutes); a
# near-idle database then ships a segment every ~60 s (measured max 61.9 s).
ARCHIVE_TIMEOUT_S = 60

# meiva:note
# The recovery-point objective: committed data older than this must be in the archive. The
# only time threshold of the archive health probe (RPO <= 5 minutes).
RPO_OBJECTIVE_S = 300


# meiva:note
# The archive settings a Postgres launched by this code carries.
@dataclass(frozen=True)
class ExpectedArchive:
    mode: str
    timeout_s: int
    command: str


# meiva:note
# The archive settings a running Postgres reports.
@dataclass(frozen=True)
class ActualArchive:
    mode: str
    timeout_s: int
    command: str


# meiva:note
# Postgres reads `%p`/`%f` in an archive command and `%%` as a literal percent.
def _percent_escaped(text: str) -> str:
    return text.replace("%", "%%")


# meiva:note
# A shell command Postgres runs: the pinned wal-g, `--config <file>`, then `walg_args`.
#
# `walg_args` are written as given (they hold Postgres' own `%p` / `%f` placeholders); the
# two paths have their `%` escaped. Used for the archive command and for the restore
# command of a recovering instance.
def postgres_command(config_file: Path, walg_binary: Path, *walg_args: str) -> str:
    return shlex.join(
        [
            _percent_escaped(str(walg_binary)),
            "--config",
            _percent_escaped(str(config_file)),
            *walg_args,
        ]
    )


# meiva:note
# The archive settings for a configured WAL-G.
def expected_settings(config_file: Path, walg_binary: Path) -> ExpectedArchive:
    return ExpectedArchive(
        mode="on",
        timeout_s=ARCHIVE_TIMEOUT_S,
        command=postgres_command(config_file, walg_binary, "wal-push", "%p"),
    )


# meiva:note
# `postgres` launch arguments for the expected archiving.
def archive_args(expected: ExpectedArchive) -> list[str]:
    return [
        "-c",
        f"archive_mode={expected.mode}",
        "-c",
        f"archive_timeout={expected.timeout_s}s",
        "-c",
        f"archive_command={expected.command}",
    ]


# meiva:note
# `archive_mode` is compared literally, so a standby running `always` would report a
# mismatch on every start. No Meiva home uses `always` today; a standby profile would
# have to teach this comparison about it first.
def differs(actual: ActualArchive, expected: ExpectedArchive) -> bool:
    return (actual.mode, actual.timeout_s, actual.command) != (
        expected.mode,
        expected.timeout_s,
        expected.command,
    )


# meiva:note
# The start-time alert: the machine, both value sets, and what to do about it.
def mismatch_message(machine: str, actual: ActualArchive, expected: ExpectedArchive) -> str:
    return (
        f"WAL archiving: {machine} started Postgres without the configured archive settings — "
        f"archive_mode={actual.mode} (expected {expected.mode}), "
        f"archive_timeout={actual.timeout_s}s (expected {expected.timeout_s}s), "
        f"archive_command={actual.command!r} (expected {expected.command!r}). "
        "Stop this unit with `meiva stop`, then start it again with `meiva start` and its "
        "explicit service selection."
    )
