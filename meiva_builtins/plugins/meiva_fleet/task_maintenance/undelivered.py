# meiva:note
# The blind spot behind the reminder counters: delivery is what advances a task's reminder
# counter, so a delivery path that stops leaves nothing to notice -- the sweep stays quiet
# because it never sent, the counters stay quiet because they never advanced, and the task
# simply sits (task #6399, the gap #8596 exposed). This module answers the one question the
# counters cannot: has a reminder reached this owner at all?
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base import telemetry
from base.db.rows import query_all

# meiva:note
# A task counted here stayed overdue past its interval plus the grace with `reminder_count`
# still zero, so no reminder ever reached its owner. The other clauses keep the normal reasons
# a reminder is held back out of the count -- a paused task, a task waiting behind a live child,
# a terminated owner that is rerouted instead -- so the number reads zero every sweep while
# delivery works and turns non-zero the moment it stops. The grace is the caller's: the sweep
# interval plus the longest digest hold, past which a working path has already counted.
_UNDELIVERED_SQL = """
    SELECT t.owner, count(*)::bigint AS undelivered
    FROM agent_tasks t
    JOIN agents_meta m ON m.id = t.owner
    WHERE t.status = 'in_progress'
      AND t.owner IS NOT NULL
      AND t.remind_interval_seconds IS NOT NULL
      AND NOT t.is_root
      AND t.reminder_count = 0
      AND m.status <> 'terminated'
      AND (t.reminders_paused_until IS NULL OR t.reminders_paused_until <= now())
      AND NOT EXISTS (
          SELECT 1 FROM agent_tasks child
          WHERE child.parent_id = t.id AND child.status = 'in_progress'
      )
      AND now() - t.updated_at > make_interval(secs => t.remind_interval_seconds + %s)
    GROUP BY t.owner
"""


# meiva:note
# One owner's count, named as the statement selects it.
@dataclass(frozen=True)
class _UndeliveredRow:
    owner: int
    undelivered: int


# meiva:note
# One event per owner with at least one task that stayed overdue and never got a reminder: the
# count is the reading, so an owner with none stays silent rather than reporting zero.
def report_undelivered(pool: ConnectionPool, grace_seconds: float) -> None:
    with pool.connection() as conn:
        rows = query_all(conn, _UndeliveredRow, _UNDELIVERED_SQL, (grace_seconds,))
    for row in rows:
        telemetry.emit(
            "telemetry",
            "task_reminder_undelivered",
            agent_id=row.owner,
            source="system",
            attributes={"owner_id": row.owner, "count": row.undelivered},
        )
