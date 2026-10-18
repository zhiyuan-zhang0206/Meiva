from __future__ import annotations

import uuid
from typing import Any

from base.events.notifications.publish import deliver_async


async def test_delivery_records_one_row(aops_pool: Any) -> None:
    record = await deliver_async(
        aops_pool,
        category_id="core.turn_finished",
        subject="Agent #1 finished its turn",
        agent_id=1,
    )
    assert record is not None
    assert record.id > 0
    assert record.category == "core.turn_finished"
    assert record.importance == "normal"


async def test_disabled_category_records_nothing(aops_pool: Any) -> None:
    record = await deliver_async(
        aops_pool,
        category_id="core.turn_finished",
        subject="Agent #1 finished its turn",
        overrides={"core.turn_finished": False},
    )
    assert record is None


async def test_same_key_inside_the_window_is_dropped(aops_pool: Any) -> None:
    key = f"notice:{uuid.uuid4()}"
    first = await deliver_async(
        aops_pool,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        dedupe_key=key,
    )
    second = await deliver_async(
        aops_pool,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        dedupe_key=key,
    )
    assert first is not None
    assert second is None


async def test_unknown_category_is_rejected(aops_pool: Any) -> None:
    try:
        await deliver_async(aops_pool, category_id="plugin.unknown", subject="x")
    except ValueError as exc:
        assert "unknown notification category" in str(exc)
    else:
        raise AssertionError("an unknown category must fail fast")


async def test_empty_subject_is_rejected(aops_pool: Any) -> None:
    try:
        await deliver_async(aops_pool, category_id="core.turn_finished", subject="  ")
    except ValueError as exc:
        assert "subject" in str(exc)
    else:
        raise AssertionError("an empty subject must fail fast")
