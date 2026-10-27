# meiva:note
# One inbound for work a harness session reports after its turn ended — a background
# command finishing, an answer the CLI wrote on its own. The session holds no inbox,
# so the host writes the row; this module owns its shape, beside the other shapes one
# inbound can take.
# meiva:note
# `system:harness` reads as a framework note in the envelope the claim node wraps it
# in, which is what this is: neither the user nor a peer agent sent it, and the agent
# may not mistake it for either. It is a `chat` inbound — not `system_note` — because
# the point is to run a turn: the report is work the agent asked for, and a note
# nobody must answer would leave the agent exactly as unaware as before (#5663). The
# insert publishes its own wake, the way every other inbound does.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
from __future__ import annotations

from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.events.live.bus import EventBus

HARNESS_NOTICE_SOURCE = "system:harness"


def insert_harness_notice(db: Database, bus: EventBus, agent_id: int, text: str) -> int:
    with db.write_transaction() as conn:
        return insert_inbound_message(
            conn, agent_id, text, HARNESS_NOTICE_SOURCE, database=db, bus=bus
        )
