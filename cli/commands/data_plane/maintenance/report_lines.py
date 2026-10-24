# meiva:note
# The lines the maintenance stop reports to the operator and the stop journal, as values.
#
# They are report-only: the pooler stops by SIGINT, which disconnects its clients, so nothing
# here gates or changes the stop. The shell prints, logs and appends what these return.

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["escalation_note", "pooler_clients_line", "pooler_console_unreadable_line"]


# meiva:note
# The line naming the remote clients still connected to the pooler when it stops, or None
# when there are none. `descriptions` is one rendered line per client; at most `shown` are
# listed and the rest are counted.
def pooler_clients_line(descriptions: Sequence[str], *, shown: int) -> str | None:
    if not descriptions:
        return None
    listed = "; ".join(descriptions[:shown])
    more = f"; +{len(descriptions) - shown} more" if len(descriptions) > shown else ""
    return f"{len(descriptions)} client(s) still connected when the pooler stops: {listed}{more}"


# meiva:note
# A console that cannot be read is reported as that, never as an empty list.
def pooler_console_unreadable_line(error: str) -> str:
    return f"pooler clients could not be listed before the pooler stop: {error}"


# meiva:note
# A Postgres fast shutdown that had to be ended by an immediate one, for the operator and the
# stop journal. The owner of the shutdown (`base.cluster.postgres`) logs the escalation itself.
def escalation_note(detail: str, killed: Sequence[int]) -> str:
    leftover = ", ".join(str(pid) for pid in killed) or "none"
    return (
        f"postgres {detail}; ended by an immediate shutdown "
        f"(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        f"killed leftover processes: {leftover}"
    )
