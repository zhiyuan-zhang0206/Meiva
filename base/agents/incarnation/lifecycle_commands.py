# meiva:note
# Payload markers, resurrection supersession and receipts of restart/terminate command rows.
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


# meiva:note
# Resurrection needs positive evidence that no earlier incarnation can still
# write (docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md). A row this
# runtime terminated while no incarnation owned it carries that evidence as two
# facts only this runtime writes, both new values in existing columns:
#
# - its unowned state has a lifecycle origin: a birth recorded as epoch 0
#   (`agents_meta.last_resurrect_inbound_id`, stamped by the spawn INSERT), or
#   a `lifecycle_release` receipt on the resurrect or restart inbound whose
#   transition left the row unowned (a resurrection or an applied restart). A
#   retired runtime left identical row shapes but never these values, so a
#   legacy unowned row has no origin;
# - the force that ended it recorded `unowned_termination` on its own command.
#
# Like the markers above, only the exact JSON boolean `true` counts, and the
# fragments embed the keys literally.
LIFECYCLE_RELEASE = "lifecycle_release"
UNOWNED_TERMINATION = "unowned_termination"

# meiva:note
# The unaliased agents_meta row's unowned state came from this runtime's lifecycle.
_LIFECYCLE_ORIGIN: LiteralString = (
    "(agents_meta.last_resurrect_inbound_id = 0 OR EXISTS (SELECT 1 FROM inbound_messages r "
    "WHERE r.agent_id = agents_meta.id AND r.kind IN ('resurrect','restart') "
    "AND COALESCE((r.payload -> 'lifecycle_release') = 'true'::jsonb, false)))"
)

# meiva:note
# The force that ended the current life of the unaliased agents_meta row while
# no incarnation owned it, or NULL. A life ends once: the next resurrection
# inbound is newer than the force, so an earlier life's receipt never carries over.
UNOWNED_TERMINATION_ID: LiteralString = (
    "(SELECT max(u.id) FROM inbound_messages u WHERE u.agent_id = agents_meta.id "
    "AND u.kind = 'terminate' AND u.id > COALESCE((SELECT max(r.id) FROM inbound_messages r "
    "WHERE r.agent_id = agents_meta.id AND r.kind = 'resurrect'), 0) "
    "AND COALESCE((u.payload -> 'unowned_termination') = 'true'::jsonb, false))"
)

# meiva:note
# That receipt named by its id (the one `%s`) on the unaliased agents_meta row.
# A command row is immutable evidence: a transaction's own resurrect inbound
# cannot satisfy this.
UNOWNED_TERMINATION_RECORDED: LiteralString = (
    "EXISTS (SELECT 1 FROM inbound_messages u WHERE u.id = %s AND u.agent_id = agents_meta.id "
    "AND u.kind = 'terminate' "
    "AND COALESCE((u.payload -> 'unowned_termination') = 'true'::jsonb, false))"
)

# meiva:note
# Built from constant fragments only: no caller input reaches this statement.
_RECORD_UNOWNED_TERMINATION: LiteralString = (
    "UPDATE inbound_messages u SET payload=COALESCE(u.payload,'{}'::jsonb)||%s "  # noqa: S608
    "FROM agents_meta WHERE u.id=%s AND u.agent_id=%s AND u.kind='terminate' "
    "AND u.status='pending' AND agents_meta.id=u.agent_id AND agents_meta.status='idling' "
    "AND agents_meta.runtime_kind IS NULL AND agents_meta.runtime_generation IS NULL "
    "AND agents_meta.runtime_owner IS NULL AND agents_meta.pid IS NULL AND " + _LIFECYCLE_ORIGIN
)


# meiva:note
# Record that force `force_id` ends a row no runtime incarnation owns.
#
#     Called by the force transaction under the agents_meta row lock, before its
#     terminated write. The row must still be idling with no runtime kind,
#     generation, owner or pid, and its unowned state must have a lifecycle
#     origin. Admission writes the identity under the same lock, so no
#     incarnation is admitted concurrently. Returns whether the receipt was
#     written: a force on an owned, non-idling, already terminated or legacy
#     unowned row records nothing, and resurrection keeps refusing that row.
def record_unowned_termination(conn: psycopg.Connection, agent_id: int, force_id: int) -> bool:
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("unowned termination requires an explicit transaction")
    recorded = conn.execute(
        _RECORD_UNOWNED_TERMINATION, (Jsonb({UNOWNED_TERMINATION: True}), force_id, agent_id)
    )
    return recorded.rowcount == 1
