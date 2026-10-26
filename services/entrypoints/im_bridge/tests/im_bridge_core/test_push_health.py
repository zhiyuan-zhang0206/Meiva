from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from psycopg_pool import ConnectionPool

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.entrypoints.im_bridge.outbound.health import (
    PUSH_HEALTH_NEVER_SENT,
    emit_push_health_once,
    push_health_loop,
)
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore
from services.entrypoints.im_bridge.tests.slices import im_bridge_config, system_clock


class _Store:
    def __init__(self, snapshot: dict[str, tuple[int, datetime | None]]) -> None:
        self._snapshot = snapshot

    def push_health_snapshot(
        self, channels: tuple[str, ...]
    ) -> dict[str, tuple[int, datetime | None]]:
        return {ch: v for ch, v in self._snapshot.items() if ch in channels}


def _fake_store(snapshot: dict[str, tuple[int, datetime | None]]) -> IMOutboxStore:
    return cast(IMOutboxStore, _Store(snapshot))


async def test_emit_once_reports_age_and_pending_per_leg(loguru_records: LogRecords) -> None:
    timers = ManualTimers()
    clock = Clock(
        ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=timers, now=timers.now
    )
    now = timers.now()
    store = _fake_store(
        {
            "telegram": (3, now - timedelta(seconds=125.0)),
            "feishu": (0, now - timedelta(seconds=90.0)),
        }
    )
    config = im_bridge_config(im_push_health_channels=("telegram", "feishu"))

    await emit_push_health_once(store, config, clock=clock)

    events = loguru_records.events("im_push_health")
    assert [(e["extra"]["channel"], e["extra"]["pending"]) for e in events] == [
        ("telegram", 3),
        ("feishu", 0),
    ]
    assert [e["extra"]["last_ok_age_s"] for e in events] == [125.0, 90.0]


async def test_emit_once_reports_never_sent_sentinel(loguru_records: LogRecords) -> None:
    store = _fake_store({"telegram": (2, None)})
    config = im_bridge_config(im_push_health_channels=("telegram",))

    await emit_push_health_once(store, config, clock=system_clock())

    (event,) = loguru_records.events("im_push_health")
    assert event["extra"]["last_ok_age_s"] == PUSH_HEALTH_NEVER_SENT
    assert event["extra"]["pending"] == 2


async def test_loop_samples_once_per_interval(
    loguru_records: LogRecords, pool: ConnectionPool
) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    config = im_bridge_config(
        im_push_health_channels=("feishu",), im_push_health_interval_seconds=0.02
    )
    sampling = asyncio.create_task(push_health_loop(store, config, clock=system_clock()))
    try:
        for _ in range(200):
            if len(loguru_records.events("im_push_health")) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        sampling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sampling
    events = loguru_records.events("im_push_health")
    assert len(events) >= 3
    assert {event["extra"]["channel"] for event in events} == {"feishu"}


def test_snapshot_counts_waiting_and_last_sent(pool: ConnectionPool) -> None:
    store = IMOutboxStore(pool, new_id=uuid4)
    channel = "test-health-snapshot"
    sent_at = datetime.now(UTC) - timedelta(seconds=42)
    with pool.connection() as conn:
        conn.execute("DELETE FROM im_bridge_outbound_intents WHERE channel = %s", (channel,))
        for status, attempt in (
            ("queued", None),
            ("sending", uuid4()),
            ("sent", uuid4()),
            ("uncertain", uuid4()),
            ("failed", uuid4()),
        ):
            conn.execute(
                "INSERT INTO im_bridge_outbound_intents "
                "(channel, account_id, chat_id, agent_id, source_kind, source_id, block_idx, "
                "request, status, attempt_id, completed_at) "
                "VALUES (%s,'acct','chat',1,'message',%s,0,'{}'::jsonb,%s,%s,%s)",
                (channel, f"{status}-row", status, attempt, sent_at if status == "sent" else None),
            )

    snapshot = store.push_health_snapshot([channel, "test-health-empty"])

    pending, last_ok = snapshot[channel]
    assert pending == 2
    assert last_ok is not None and abs((last_ok - sent_at).total_seconds()) < 1
    assert "test-health-empty" not in snapshot
