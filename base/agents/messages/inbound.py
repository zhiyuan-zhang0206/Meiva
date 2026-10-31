"""Inbound-queue primitives shared across layers.

`InboundKind` is the piece of the inbound queue that layers below the kernel
reach for: the timeline renderer matches on it and the claim node dispatches
on it. The transactional claim / reconcile / status-transition SQL stays in
`agent/db/__init__.py` (kernel-only, async).

Wake is Redis pub/sub: `insert_inbound_message` (`base/agents/inbox/insert.py`) and the
transactionally keyed chat path (`base/agents/inbox/chat_delivery.py`) publish to
the cluster-scoped channel `<prefix>:inbound:<agent_id>`
(`base.cluster.inbound_channel`) on every inbound INSERT, and the claim node
blocks on `RedisInboundListener.wait_one(timeout=...)` (`base/events/live/redis_listener.py`)
until a publish arrives. There is no module-level channel constant because the
channel is per-agent and cluster-scoped — a hardcoded prefix would fall outside
a dev cluster's `&<prefix>:*` redis ACL grant.
"""

from enum import StrEnum
from typing import Literal, LiteralString, get_args


class InterruptReason(StrEnum):
    """Attribution of an observed interrupt; claim still owns its semantics."""

    USER = "user"
    SYSTEM = "system"
    RESTART = "restart"


class InboundKind(StrEnum):
    """Legal values for inbound_messages.kind — in sync with the CHECK constraint in db/schema.sql.

    StrEnum lets the enum value be both a str (can be passed to SQL) and comparable
    to literals. The claim node's `match item.kind:` uses `case InboundKind.CHAT:`
    so pyright statically exhaustively checks (when a new kind is added but
    dispatch isn't synced, pyright reports "unmatched"). Same pattern as
    `base.api_contracts.agent_contract.AgentStatus(StrEnum)`.
    """

    CHAT = "chat"
    SYSTEM_NOTE = "system_note"
    COMPACT_SUMMARY = "compact_summary"
    COMPACT_REQUEST = "compact_request"
    CANCEL = "cancel"
    TERMINATE = "terminate"
    RESTART = "restart"
    RESURRECT = "resurrect"
    FORK = "fork"
    HEARTBEAT = "heartbeat"
    REMINDER = "reminder"


# Only these pending-work kinds can trigger automatic resurrection.
WakeTriggerKind = Literal[
    InboundKind.CHAT, InboundKind.COMPACT_REQUEST, InboundKind.SYSTEM_NOTE, InboundKind.HEARTBEAT
]


def validate_wake_trigger_kind(value: str) -> WakeTriggerKind:
    """Parse a wake trigger without admitting unrelated inbound control kinds."""
    kind = InboundKind(value)
    for allowed in get_args(WakeTriggerKind):
        if kind is allowed:
            return allowed
    raise ValueError(f"inbound kind cannot trigger resurrection: {value!r}")


# meiva:note
# Payload flag marking a passive system_note (e.g., host update/stop/restart explanation
# recorded for idle agents): delivered on the agent's next real wake without triggering a
# wake scan, suppressing heartbeats, or resetting heartbeat backoff.
PASSIVE_INBOUND_KEY = "passive"


def not_passive_inbound(table_alias: LiteralString = "") -> LiteralString:
    prefix = f"{table_alias}." if table_alias else ""
    return f"NOT COALESCE(({prefix}payload -> 'passive') = 'true'::jsonb, false)"
