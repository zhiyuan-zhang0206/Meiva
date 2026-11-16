from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from base.daemon.schedules.watcher import CronExprError
from gateway.schedules.contracts import ScheduleCreate
from gateway.schedules.triggers import message_script, prepare

_NOW = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)


def test_every_prepares_next_slot_and_owner_cancel_default() -> None:
    body = ScheduleCreate(
        name="periodic",
        trigger_kind="every",
        interval_seconds=90,
        script="pass\n",
        owner_agent_id=42,
    )

    prepared = prepare(body, cluster_timezone="Asia/Shanghai", now=_NOW)

    assert prepared.next_launch_at == _NOW + timedelta(seconds=90)
    assert prepared.timezone == "Asia/Shanghai"
    assert prepared.on_owner_terminate == "cancel"


def test_cron_uses_declared_timezone() -> None:
    body = ScheduleCreate(
        name="midnight",
        trigger_kind="cron",
        cron_expr="0 0 * * *",
        timezone="Asia/Shanghai",
        script="pass\n",
    )

    prepared = prepare(body, cluster_timezone="UTC", now=_NOW)

    assert prepared.next_launch_at == datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
    assert prepared.on_owner_terminate == "keep"


def test_missed_at_keeps_past_slot_for_one_recovery_fire() -> None:
    missed = _NOW - timedelta(hours=2)
    body = ScheduleCreate(name="once", trigger_kind="at", run_at=missed, script="pass\n")

    assert prepare(body, cluster_timezone="UTC", now=_NOW).next_launch_at == missed


def test_message_payload_is_minimal_plain_python() -> None:
    assert message_script(42, "wake") == ("import meiva\n\nmeiva.agents.send_message(42, 'wake')\n")


# meiva:note
# The table deliberately includes a naive datetime, and the dynamic kwargs are the wire shapes
# this test asks Pydantic to reject before typed application code can receive them.
@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger_kind": "every", "interval_seconds": 29, "script": "pass"},
        {"trigger_kind": "every", "interval_seconds": 60, "script": "pass", "message": "x"},
        {"trigger_kind": "cron", "cron_expr": "0 * * * *", "script": None, "message": "x"},
        {
            "trigger_kind": "at",
            "run_at": datetime(2026, 11, 17),  # noqa: DTZ001
            "script": "pass",
        },
    ],
)
def test_invalid_wire_shapes_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScheduleCreate(name="invalid", **kwargs)  # pyright: ignore[reportArgumentType]


def test_invalid_cron_and_timezone_are_rejected_during_preparation() -> None:
    invalid_cron = ScheduleCreate(
        name="bad-cron", trigger_kind="cron", cron_expr="bad", script="pass"
    )
    with pytest.raises(CronExprError):
        prepare(invalid_cron, cluster_timezone="UTC", now=_NOW)

    invalid_timezone = ScheduleCreate(
        name="bad-zone",
        trigger_kind="every",
        interval_seconds=60,
        timezone="Not/A_Zone",
        script="pass",
    )
    with pytest.raises(ValueError):
        prepare(invalid_timezone, cluster_timezone="UTC", now=_NOW)
