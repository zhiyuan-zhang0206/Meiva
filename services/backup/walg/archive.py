"""What Postgres is told about WAL archiving, in one place.

The settings are launch arguments (`-c`) of the cluster's postmaster, never
`ALTER SYSTEM` and never a line in `postgresql.conf`: the code is the only source
of truth, nothing is written into the data directory (so a base backup carries
no archive setting and a restored instance cannot archive into the live chain by
accident), and turning WAL-G off is "unset the key, restart" with nothing left behind.

`archive_mode` is a postmaster-context setting: it only takes effect when Postgres
is newly launched. A retained postmaster that is merely reloaded keeps its old
launch arguments, so switching archiving on (or off) is an explicit
`meiva stop` + `meiva start`; the health probe reports a Postgres that disagrees with
the configured expectation.

The command itself holds no secret: the pinned binary, `--config <file>` and
`wal-push %p`. WAL-G reads the credentials from the 0600 file on every call.
"""

from __future__ import annotations

from pathlib import Path

from base.cluster.dataplane.walg_binary import walg_path
from services.backup.walg.archive_plan import (
    ExpectedArchive,
    archive_args,
    expected_settings,
)
from services.backup.walg.archive_plan import postgres_command as command_text
from services.backup.walg.config import WalgConfigError


def postgres_command(*walg_args: str, path: Path | None) -> str:
    """A shell command Postgres runs: the pinned wal-g, `--config <file>`, then `walg_args`.

    `walg_args` are written as given (they hold Postgres' own `%p` / `%f`
    placeholders); the two paths have their `%` escaped. Used for the archive
    command and for the restore command of a recovering instance.

    Raises:
        WalgConfigError: WAL-G is not configured.
    """
    if path is None:
        raise WalgConfigError("MEIVA_WALG_CONFIG_FILE is not set")
    return command_text(path, walg_path(), *walg_args)


# meiva:note
# The archive settings for the WAL-G configured at `path`, or None when WAL-G is off.
def expected_for(path: Path | None) -> ExpectedArchive | None:
    return None if path is None else expected_settings(path, walg_path())


def expected_archive(*, path: Path | None) -> ExpectedArchive | None:
    """The archive settings for the configured WAL-G, or None when WAL-G is off."""
    return expected_for(path)


def archive_pg_args(*, path: Path | None) -> list[str]:
    """`postgres` launch arguments for the configured archiving; empty when WAL-G is off."""
    expected = expected_archive(path=path)
    return [] if expected is None else archive_args(expected)
