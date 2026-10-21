# meiva:note
# The owner message every reclaim shares: one system inbound written in the
# caller's transaction, woken only for a live owner.
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import psycopg

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.messages.inbound_provenance import InboundProvenance
from base.clock import Clock
from base.db import Database
from base.events.live.bus import EventBus

# Batch ceiling per pass: the tables are small by design; the cap keeps a
# backlog (e.g. after a long outage) from turning one pass into a multi-minute
# transaction.
PASS_BATCH = 200

# meiva:note
# The states that can act on a reclaim message right now. Any other state (today
# only `terminated`) is queued only for a message reporting the agent's own
# notice; a page or shell reminder would open a later life on work that is gone.
_NOTIFIABLE_STATUSES = ("running", "idling")


# meiva:note
# `live` records whether the owner was running or idling when the row was
# written: only such an owner is woken, and only after the caller committed.
@dataclass(frozen=True)
class OwnerMessage:
    agent_id: int
    inbound_id: int
    live: bool


# meiva:note
# A terminated owner is skipped unless `queue_when_terminated`. A page or shell
# message tells the agent to stop a server or that a job died, so delivering it
# to a later life would open that life on work that no longer exists; notice
# expiry passes the flag because its message reports the owner's own notice.
# Even then nothing wakes a dead owner, and no notification path resurrects one:
# every `system:` trigger is refused by `ops.lifecycle.resurrect_gates`.
def notify_owner(
    conn: psycopg.Connection,
    agent_id: int,
    content: str,
    *,
    source: str = "system",
    queue_when_terminated: bool = False,
) -> OwnerMessage | None:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None:
            return None
        live = row[0] in _NOTIFIABLE_STATUSES
        if not live and not queue_when_terminated:
            return None
        # meiva:note
        # A system-sourced chat produces no lineage event, so only the wake is
        # left for the caller.
        inbound_id, _event = insert_inbound_message_in_transaction(
            cur,
            agent_id,
            content,
            source=source,
            provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
        )
    return OwnerMessage(agent_id=agent_id, inbound_id=inbound_id, live=live)


# meiva:note
# The caller commits first. A wake published before that commit rouses an agent
# whose claim query cannot see the row yet.
def wake_owners(db: Database, bus: EventBus, messages: Iterable[OwnerMessage]) -> None:
    for message in messages:
        if message.live:
            publish_inbound_wake(db, bus, message.agent_id, str(message.inbound_id))


# meiva:note
# Local wall-clock for the message: HH:MM, or MM-DD HH:MM when the moment is not
# on the configured zone's today (a TTL can cross midnight). A missing zone
# falls back to the host zone (``dt.astimezone(None)``), the base/config
# contract for "no zone configured".
def wall_clock(dt: datetime) -> str:
    tz = Clock.from_settings().zone()
    local = dt.astimezone(tz)
    stamp = local.strftime("%H:%M")
    if local.date() != datetime.now(tz).date():
        stamp = f"{local.strftime('%m-%d')} {stamp}"
    return stamp
