# meiva:note
# Passive host-lifecycle system notes for live idle agents on a stopping or restarting machine.
#
# When an operator stops or restarts a host (or updates the cluster) with `--message`, running
# turns receive the explanation through `GracefulExit`'s `restart` command, while live idle
# agents on that machine receive one passive `system_note` (`payload.passive = true`). A passive
# note publishes no wake and is ignored by the pending-wake scanner and heartbeat suppression, so
# a cluster update never triggers a thundering-herd wakeup or breaks prompt prefix caches; the
# agent claims the note in `created_at` order on its next real wake.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from base.agents.messages.inbound import PASSIVE_INBOUND_KEY
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import restart_accepted_text

__all__ = ["insert_passive_host_lifecycle_notes"]


def insert_passive_host_lifecycle_notes(
    conn: psycopg.Connection[Any],
    *,
    machine: str,
    source: str,
    message: str,
    exclude_agent_ids: Collection[int] = (),
) -> int:
    text = message.strip()
    if not text:
        raise ValueError("passive lifecycle note message must be non-empty")
    note_content = restart_accepted_text(source, text)
    payload = Jsonb(
        {
            "note_tag": NoteTag.LIFECYCLE_RESTART.value,
            PASSIVE_INBOUND_KEY: True,
        }
    )
    excluded = sorted(set(exclude_agent_ids))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "SELECT m.id, %s, 'system_note', %s, %s "
            "FROM agents_meta m "
            "WHERE m.machine = %s "
            "  AND m.status IN ('idling', 'running') "
            "  AND NOT (m.id = ANY(%s::int[])) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM inbound_messages lc "
            "    WHERE lc.agent_id = m.id "
            "      AND lc.kind IN ('restart', 'terminate') "
            "      AND lc.status IN ('pending', 'claimed')"
            "  )",
            (note_content, source, payload, machine, excluded),
        )
        return cur.rowcount
