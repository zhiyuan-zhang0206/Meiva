# meiva:note
# The durable gates read before an automatic wake: the recovery breaker and the trigger's source.

from __future__ import annotations

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message
from base.api_contracts.agent_contract import AgentNotFound
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from ops import lifecycle


# meiva:note
# A system-family chat is a notice (plain and variant); a user chat, a system_note and a watcher
# wake are not, and a missing row or a foreign agent reads as no notice. The payload marker is a
# fail-closed carve-out: only the exact JSON boolean `true` lets a system-family chat through, and
# a missing key, null or any other value (even the string "true") stays a notice (task #3687
# review, Meiva #3242).
def test_the_trigger_gate_reads_the_rows_kind_source_and_payload(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    aid = create_agent(db_conn)
    db_conn.commit()
    sys_iid = insert_inbound_message(
        db_conn, aid, "notice", source="system", bus=event_bus, database=database
    )
    var_iid = insert_inbound_message(
        db_conn, aid, "variant", source="system:notice-reply", bus=event_bus, database=database
    )
    user_iid = insert_inbound_message(
        db_conn, aid, "hi", source="user", bus=event_bus, database=database
    )
    note_iid = insert_inbound_message(
        db_conn,
        aid,
        "note",
        source="system",
        kind="system_note",
        bus=event_bus,
        database=database,
    )
    # meiva:note
    # A watcher's wake is source="watcher:<id>" — neither "system" nor a "system:" variant, so it
    # is NOT a notice: a terminated owner with a live watcher is auto-resurrected at its next
    # fire, same as any user chat
    # (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md).
    watcher_iid = insert_inbound_message(
        db_conn, aid, "wake", source="watcher:7", bus=event_bus, database=database
    )

    assert lifecycle.system_notice_source_of_trigger(database, aid, sys_iid) == "system"
    assert (
        lifecycle.system_notice_source_of_trigger(database, aid, var_iid) == "system:notice-reply"
    )
    assert lifecycle.system_notice_source_of_trigger(database, aid, user_iid) is None
    assert lifecycle.system_notice_source_of_trigger(database, aid, note_iid) is None
    assert lifecycle.system_notice_source_of_trigger(database, aid, watcher_iid) is None
    assert lifecycle.system_notice_source_of_trigger(database, aid, 10**12) is None
    assert lifecycle.system_notice_source_of_trigger(database, aid + 999, sys_iid) is None

    recovery_iid = insert_inbound_message(
        db_conn,
        aid,
        "continue",
        source="system",
        payload={"hosted_turn_recovery": True},
        bus=event_bus,
        database=database,
    )
    assert lifecycle.system_notice_source_of_trigger(database, aid, recovery_iid) == "system"
    user_marker_iid = insert_inbound_message(
        db_conn,
        aid,
        "hi",
        source="user",
        payload={"hosted_turn_recovery": True},
        bus=event_bus,
        database=database,
    )
    assert lifecycle.system_notice_source_of_trigger(database, aid, user_marker_iid) is None

    fail_closed: tuple[tuple[str, dict[str, object] | None], ...] = (
        ("payload-absent", None),
        ("key-absent", {"content_blocks": []}),
        ("json-null", {"hosted_turn_recovery": None}),
        ("boolean-false", {"hosted_turn_recovery": False}),
        ("string-true", {"hosted_turn_recovery": "true"}),
        ("number-1", {"hosted_turn_recovery": 1}),
    )
    for label, payload in fail_closed:
        iid = insert_inbound_message(
            db_conn,
            aid,
            "noticed",
            source="system",
            payload=payload,
            bus=event_bus,
            database=database,
        )
        assert lifecycle.system_notice_source_of_trigger(database, aid, iid) == "system", label


# meiva:note
# The breaker trips on the stored streak of consecutive permanent provider rejections, and an
# unknown agent is an error rather than an untripped breaker.
def test_the_recovery_gate_reads_the_stored_streak(
    db_conn: psycopg.Connection, database: Database
) -> None:
    aid = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta (id,status) VALUES (%s,'terminated')", (aid,))
    db_conn.commit()

    assert lifecycle.recovery_halted(database, aid) is False

    db_conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak=1,last_permanent_reject_reason='billing' "
        "WHERE id=%s",
        (aid,),
    )
    db_conn.commit()

    assert lifecycle.recovery_halted(database, aid) is True
    with pytest.raises(AgentNotFound):
        lifecycle.recovery_halted(database, aid + 999)
