from __future__ import annotations

import psycopg

from base.agents.inbox.harness_notice import HARNESS_NOTICE_SOURCE, insert_harness_notice
from base.agents.messages.envelope import wrap_inbound
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.db import Database, create_agent
from base.events.live.bus import EventBus


def test_a_harness_report_becomes_a_chat_inbound(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    # meiva:note
    # The report a provider makes while no turn is running has exactly one way in: an
    # inbound row of its own. A `system:harness` chat inbound wakes the agent the way
    # any other message does — which is the whole point, since a note nobody has to
    # answer would leave the agent as unaware as before (task #5663).
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    agent_id = create_agent(db_conn)
    text = "Background task bhd09i69m reported completed.\nOutput file: /tmp/tasks/bhd09i69m.output"

    inbound_id = insert_harness_notice(database, event_bus, agent_id, text)

    row = db_conn.execute(
        "SELECT kind, source, content, status FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()
    db_conn.commit()
    assert row == ("chat", HARNESS_NOTICE_SOURCE, text, "pending")
    assert HARNESS_NOTICE_SOURCE == "system:harness", "the agent reads it as a framework note"
    assert (
        wrap_inbound(text, HARNESS_NOTICE_SOURCE, inputs=TimestampPolicy.off())
        == f"<system_note>\n{text}\n</system_note>"
    )
