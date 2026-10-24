# meiva:note
# `generate_label_async` against the label's owner row: a label is written once and announced
# once, and never over a label the user owns.
#
# The announcement is read back from the test's Redis by a real subscriber, and the label is the
# `agents` row. The model is a scripted one (`scripted_models`), selected by the labeler model
# name; its answer shapes and the rejection rules are covered in `test_label_rules.py` and
# `test_labeler_validity.py`.

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import recorded_events
from base.lm.catalog import ModelCatalog
from services.derived.labeler.tests.slices import generate


def _label_of(conn: psycopg.Connection, agent_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


# meiva:note
# Generate the label of agent `tid` from the scripted model `model`; return the `label_updated`
# events that reached the events channel meanwhile.
async def _generated(
    tid: int,
    model: str,
    *,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> list[dict[str, Any]]:
    with recorded_events(event_bus) as events:
        await generate(
            tid, model, bus=event_bus, catalog=model_catalog, database_gate=database_gate
        )
    return [event for event in events if event["role"] == "label_updated"]


@pytest.mark.asyncio
async def test_writes_the_label_when_null_and_publishes(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    published = await _generated(
        tid,
        "say:\u67e5 X \u6a21\u5757",
        event_bus=event_bus,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )

    assert _label_of(db_conn, tid) == "\u67e5 X \u6a21\u5757"
    assert db_conn.execute(
        "SELECT label_user_set FROM agents WHERE id = %s", (tid,)
    ).fetchone() == (False,)
    assert [(event["agent_id"], event["label"]) for event in published] == [
        (tid, "\u67e5 X \u6a21\u5757")
    ]


@pytest.mark.asyncio
async def test_a_label_the_user_set_is_kept_and_nothing_is_announced(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents SET label='\u7528\u6237\u6539\u7684', label_user_set=TRUE WHERE id=%s",
            (tid,),
        )
    db_conn.commit()

    published = await _generated(
        tid,
        "say:the model's own",
        event_bus=event_bus,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )

    assert _label_of(db_conn, tid) == "\u7528\u6237\u6539\u7684"
    assert published == []


@pytest.mark.asyncio
async def test_a_label_the_user_reset_stays_unset_and_nothing_is_announced(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label=NULL, label_user_set=TRUE WHERE id=%s", (tid,))
    db_conn.commit()

    published = await _generated(
        tid,
        "say:the model's own",
        event_bus=event_bus,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )

    assert _label_of(db_conn, tid) is None
    assert published == []
