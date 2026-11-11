# meiva:note
# Whether the installed `wal-g` binary can be removed yet.
#
# WAL-G archiving is gone, but a Postgres that was launched while it was configured carries
# `archive_mode=on` and an `archive_command` naming the pinned binary in its launch arguments.
# Those are postmaster-level settings: nothing short of a new launch changes them, and a start
# that finds the postmaster running keeps it. Removing the binary under such a postmaster would
# make every archive attempt fail, and the segments would pile up in `pg_wal` until the disk
# fills. So the binary goes only once the running Postgres is known not to archive; until then
# it stays (archiving keeps working) and the converge says how to finish.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PostgresArchive", "binary_may_go", "deferral_message"]


# meiva:note
# What the home's Postgres says about archiving right now.
#
# `running` is whether a postmaster owns the data directory. `mode` is its `archive_mode`, or
# None when it runs but the setting could not be read (an unreadable postmaster is treated as
# archiving: removing the binary is the irreversible step).
@dataclass(frozen=True)
class PostgresArchive:
    running: bool
    mode: str | None


def binary_may_go(state: PostgresArchive) -> bool:
    return not state.running or state.mode == "off"


def deferral_message(state: PostgresArchive) -> str:
    seen = "could not be read" if state.mode is None else f"is {state.mode!r}"
    return (
        f"the running Postgres still archives WAL through the retired wal-g (archive_mode {seen}); "
        "the wal-g binary stays so that archiving does not start failing. Stop and start the "
        "data plane (`meiva stop --stop-infra`, then `meiva start`) to launch Postgres without "
        "archiving; the next converge then removes the binary"
    )
