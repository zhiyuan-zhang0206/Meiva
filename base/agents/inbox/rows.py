"""Inbox read surface: the pending-chat strip and the FYI notice lifetime."""

from datetime import datetime
from typing import NamedTuple

import psycopg


class InboundRow(NamedTuple):
    """One row of inbound_messages (for timeline reads)."""

    id: int
    content: str
    kind: str
    source: str | None
    status: str
    created_at: datetime
    claimed_at: datetime | None = None
    # JSONB sidecar, None when the column is NULL. Only readers that need it
    # select the column (list_pending_inbounds — the multimodal
    # `{"content_blocks": [...]}` shape); the rest leave it defaulted.
    payload: dict[str, object] | None = None


# FYI notice (require_response=false) lifetime in the open queue: after this many
# days an unread FYI is auto-resolved ('read') — the open feed, the unread badge
# and the IM bridge stop carrying it (audit 05-gateway-lifecycle C1: open FYIs
# used to pile up forever). require_response notices NEVER expire — the user's
# answer is the only close. The expiry is applied as a query-side rule
# (created_at cutoff, `make_interval(days => ...)`) plus a lazy auto-resolve in
# the gateway's open-feed query, so no background sweeper is needed. One knob:
# change it here (and re-run the notice tests) to configure the TTL.
NOTICE_FYI_TTL_DAYS = 30


# meiva:note
# Pending chats stay in the composer strip. A claimed chat for the native
# harness moves to the conversation at claim. External chats remain visible
# here during pickup and leave when provider acceptance is committed or delivery
# uncertainty is recorded.
# The strip also needs the payload for attachments.
# meiva:link base/agents/inbox/docs/inbox.meiva.okf.md
def list_pending_inbounds(db: psycopg.Connection, agent_id: int) -> list[InboundRow]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT ib.id, ib.content, ib.kind, ib.source, ib.status, ib.created_at, "
            "       ib.claimed_at, ib.payload "
            "FROM inbound_messages ib "
            "WHERE ib.agent_id = %s AND ib.kind = 'chat' "
            "AND (ib.status = 'pending' OR (ib.status = 'claimed' AND EXISTS ("
            "  SELECT 1 FROM agents_meta am WHERE am.id = ib.agent_id AND am.harness <> 'meiva'"
            "))) "
            "ORDER BY ib.created_at ASC, ib.id ASC",
            (agent_id,),
        )
        return [InboundRow(*row) for row in cur.fetchall()]
