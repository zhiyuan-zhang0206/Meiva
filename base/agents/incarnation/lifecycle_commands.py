# meiva:note
# Payload markers and resurrection supersession of restart/terminate command rows.
#
# A restart/terminate command is an `inbound_messages` row that moves
# `pending -> claimed -> done`: the host claims it at the turn boundary and its
# apply (`agent.ownership.hosted.apply_hosted_lifecycle`) executes it and marks it
# done in one transaction. Nothing else is recorded about a command's life.

from collections.abc import Mapping
from typing import LiteralString

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

# meiva:note
# System-family chats are notifications. Historical recovery markers do not
# exempt them or revive a terminated owner. SQL and Python share this predicate.
SYSTEM_NOTICE_SOURCE: LiteralString = "(m.source = 'system' OR starts_with(m.source, 'system:'))"


# meiva:note
# Match the SQL predicate; historical payload markers grant no exception.
def is_system_notice_source(source: str, payload: Mapping[str, object] | None) -> bool:
    del payload
    return source == "system" or source.startswith("system:")


# meiva:note
# The wake-trigger fence of a terminated owner, as one SQL predicate.
#
# A trigger must be newer than every kill command the owner received, so work
# queued before a kill cannot reverse it — including a repeated force on an
# already terminated row, which changes no status but appends a command.
#
# The second half decides whether work that arrived before the death may wake the
# owner at all, and the death's own mark answers it: only a death a command ended
# (`termination_source = 'user'`) admits it, because a commanded death spends time
# between the kill decision and the process's last write, and a peer's message that
# lands in that window is work the dying incarnation never claimed. An involuntary
# death — crash, failed launch — never wakes for leftover work at any age; that is
# the no-replay boundary. An unmarked row (NULL, legacy) fails closed.
#
# Correlated against `m` and the unaliased `agents_meta` row, exactly like
# `SYSTEM_NOTICE_SOURCE`.
# meiva:link docs/decisions/agents/lifecycle/2026-07-21-crash-auto-resurrect.md
TRIGGER_AFTER_KILL_COMMAND: LiteralString = (
    "m.id > COALESCE((SELECT max(t.id) FROM inbound_messages t "
    "WHERE t.agent_id = agents_meta.id AND t.kind = 'terminate'), 0) "
    "AND (m.created_at > agents_meta.status_changed_at "
    "     OR agents_meta.termination_source = 'user')"
)


# meiva:note
# A terminate command may carry `{"kill_all_shell_sessions": true}` in its
# payload: every shell session of the agent on its home machine (watchers
# included) is killed with a graceful termination, right before it commits
# (docs/decisions/agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md). The apply
# closes every open terminate of the agent and honors the request when ANY of
# them carries it — the command the runtime claimed may be a different one (a
# self-terminate that won the claim first), and the requester's kill still rides
# that death. A force terminate kills only when asked itself.
KILL_ALL_SHELL_SESSIONS = "kill_all_shell_sessions"


# meiva:note
# Every open lifecycle command is closed by a resurrection, visibly (the payload
# names the resurrect inbound that superseded it), never silently dropped: a
# restart or terminate queued in the previous life must not act on the new one
# (issue #2158). A command created after the resurrect inbound stays current intent.
_SUPERSEDED_BY_RESURRECT: LiteralString = (
    "UPDATE inbound_messages SET status='done', "
    "payload=COALESCE(payload,'{}'::jsonb)||%s "
    "WHERE agent_id=%s AND kind IN ('restart','terminate') AND status IN ('pending','claimed') "
    "AND id < %s"
)


# meiva:note
# Close every open lifecycle command older than the resurrect inbound.
#
#     Called by the resurrection transaction under the agents_meta row lock.
def supersede_lifecycle_for_resurrect(
    conn: psycopg.Connection, agent_id: int, resurrect_id: int
) -> None:
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("resurrect supersession requires an explicit transaction")
    result = {
        "lifecycle_result": {
            "outcome": "superseded",
            "reason": "resurrect",
            "resurrect_inbound_id": resurrect_id,
        }
    }
    conn.execute(_SUPERSEDED_BY_RESURRECT, (Jsonb(result), agent_id, resurrect_id))
