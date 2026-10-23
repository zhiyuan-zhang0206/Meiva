from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import ANY, Mock

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.system.tests.fake_commands import fake_host
from base.native_process.loaded_commit import LoadedCommit
from meiva.sdk_surface import process_context
from schedules.catchup import fire_slot_once
from schedules.daily_host import DailyLoopHooks
from schedules.entry import ScheduleEntryHooks, fresh_schedule_entry

_ROOT = Path(__file__).resolve().parents[2]
_DAILY_SCRIPTS = ("c9-daily-report-schedule.py", "dev-ci-metrics-schedule.py")


def _load_daily_script(filename: str) -> ModuleType:
    path = _ROOT / "schedules" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class _LoopStoppedError(Exception):
    pass


def _insert_schedule(conn: psycopg.Connection, *, created_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, created_at) "
            "VALUES (%s, 'x', 'python schedule.py', true, 'stopped', %s) RETURNING id",
            (f"catch-up-{created_at.timestamp()}", created_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _claimed_slots(conn: psycopg.Connection, schedule_id: int) -> list[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slot_fire_at FROM schedule_fire_log "
            "WHERE schedule_id = %s ORDER BY slot_fire_at",
            (schedule_id,),
        )
        return [row[0] for row in cur.fetchall()]


def _claim_worker(
    schedule_id: int,
    slot_iso: str,
    ready: Any,
    start: Any,
    outcomes: Any,
    database_gate: ProcessDbGate,
) -> None:
    os.environ["MEIVA_SCHEDULE_ID"] = str(schedule_id)
    ready.put(True)
    if not start.wait(timeout=10):
        outcomes.put("timeout")
        return
    claimed = fire_slot_once(
        Database.from_settings(gate=database_gate),
        datetime.fromisoformat(slot_iso),
        "payload",
        fire=lambda _slot, _payload: outcomes.put("fired"),
    )
    outcomes.put("claimed" if claimed else "lost")


def test_concurrent_processes_execute_a_slot_at_most_once(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
) -> None:
    assert database_gate.min_read_due(), "spawn copies a fresh admission budget"
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_claim_worker,
            args=(schedule_id, slot.isoformat(), ready, start, outcomes),
            kwargs={"database_gate": database_gate},
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=15) is True
    start.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    observed = sorted(outcomes.get(timeout=5) for _ in range(3))
    assert observed == ["claimed", "fired", "lost"]
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_winner_receives_utc_slot_and_payload_after_claim_commits(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    env = {"MEIVA_SCHEDULE_ID": str(schedule_id)}
    local_slot = datetime(2026, 9, 6, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    payload = object()
    observed: list[tuple[datetime, object]] = []

    def fire(claimed: datetime, received: object) -> None:
        assert claimed.tzinfo is UTC
        assert received is payload
        assert _claimed_slots(db_conn, schedule_id) == [slot]
        observed.append((claimed, received))

    assert fire_slot_once(database, local_slot, payload, fire=fire, env=env)
    assert not fire_slot_once(
        database,
        slot,
        payload,
        fire=lambda _slot, _payload: pytest.fail("duplicate fire"),
        env=env,
    )
    assert observed == [(slot, payload)]


def test_nested_dispatch_keeps_each_callbacks_explicit_slot(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    outer_slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    inner_slot = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)
    observed: list[tuple[datetime, str]] = []

    def outer_fire(slot: datetime, payload: str) -> None:
        observed.append((slot, payload))
        assert fire_slot_once(
            database,
            inner_slot,
            "inner",
            schedule_id=schedule_id,
            fire=lambda slot, payload: observed.append((slot, payload)),
        )
        observed.append((slot, payload))

    assert fire_slot_once(database, outer_slot, "outer", schedule_id=schedule_id, fire=outer_fire)
    assert observed == [(outer_slot, "outer"), (inner_slot, "inner"), (outer_slot, "outer")]
    assert _claimed_slots(db_conn, schedule_id) == [outer_slot, inner_slot]


def test_claim_survives_fire_failure(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = _insert_schedule(db_conn, created_at=datetime(2026, 9, 6, 9, 30, tzinfo=UTC))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="after claim"):
        fire_slot_once(
            database,
            slot,
            None,
            schedule_id=schedule_id,
            fire=lambda _slot, _payload: (_ for _ in ()).throw(RuntimeError("after claim")),
        )

    assert not fire_slot_once(
        database,
        slot,
        None,
        schedule_id=schedule_id,
        fire=lambda _slot, _payload: pytest.fail("duplicate fire"),
    )
    assert _claimed_slots(db_conn, schedule_id) == [slot]


def test_cluster_timezone_follows_the_setting_when_it_is_read() -> None:
    from schedules.catchup import cluster_timezone

    def make_clock(tz: str) -> Clock:
        return Clock(
            ClockConfig(
                timezone=tz,
                authoritative_timezone=tz,
                message_timestamp_weekday=False,
            ),
            timers=SYSTEM_TIMERS,
        )

    assert cluster_timezone() == settings.general.timezone
    assert cluster_timezone(clock_factory=lambda: make_clock("Asia/Kathmandu")) == "Asia/Kathmandu"
    assert (
        cluster_timezone(clock_factory=lambda: make_clock("America/Los_Angeles"))
        == "America/Los_Angeles"
    )


@pytest.mark.parametrize(
    "filename",
    [
        "adversarial-eval-weekly-schedule.py",
        "c9-daily-report-schedule.py",
        "debt-sweep-daily-schedule.py",
        "dev-ci-metrics-schedule.py",
        "memory-steward-schedule.py",
        "model-update-tracker-schedule.py",
        "self-evolution-daily-schedule.py",
        "self-evolution-weekly-schedule.py",
    ],
)
def test_schedule_entry_retains_the_loaded_image_gate(filename: str, tmp_path: Path) -> None:
    module = _load_daily_script(filename)
    image = LoadedCommit(source_root=tmp_path, sha="loaded-before-checkout-moved")
    capture = Mock(return_value=image)
    count = Mock(return_value=7)
    database = Mock()
    create = Mock(return_value=database)
    consumer = Mock(side_effect=_LoopStoppedError)

    def due_slot(*_args: object, **_kwargs: object) -> datetime:
        return datetime.now(UTC) - timedelta(seconds=1)

    entry = getattr(module, "_main_loop", None) or module.main
    hooks = ScheduleEntryHooks(
        image_loader=capture,
        process_name_fn=lambda: "schedule",
        database_cls=create,
        commit_counter=count,
    )

    with pytest.raises(_LoopStoppedError), fresh_schedule_entry(hooks=hooks) as inputs:
        if filename == "c9-daily-report-schedule.py":
            entry(inputs=inputs, host=fake_host(), loop_fn=consumer)
        elif filename == "dev-ci-metrics-schedule.py":
            entry(inputs=inputs, loop_fn=consumer)
        else:
            entry(inputs=inputs, next_fire_fn=due_slot, claim_fn=consumer)

    capture.assert_called_once_with()
    create.assert_called_once()
    gate = create.call_args.kwargs["gate"]
    assert isinstance(gate, ProcessDbGate)
    assert consumer.call_args.args[0] is database
    count.assert_not_called()
    assert gate.application_name() == "meiva:schedule:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    gate.observe_minimum(7)
    assert not gate.min_read_due()
    assert gate.application_name() == "meiva:schedule:v7"
    count.assert_called_once()


@pytest.mark.parametrize("filename", _DAILY_SCRIPTS)
def test_daily_loop_skips_an_already_seen_slot_and_sleeps_until_next_fire(
    filename: str, machine_identity: MachineIdentity
) -> None:
    module = _load_daily_script(filename)
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 6, 10, 1, tzinfo=UTC)
    next_slot = datetime(2026, 9, 6, 10, 2, tzinfo=UTC)
    clock = iter((start, now))

    next_fire_call = Mock(side_effect=(start, next_slot))
    claim = Mock()
    sleep = Mock(side_effect=_LoopStoppedError)
    loop_hooks = DailyLoopHooks(
        now_fn=lambda: next(clock),
        next_fire_fn=next_fire_call,
        claim_fn=claim,
        sleep_fn=sleep,
    )

    command_host = fake_host()
    with pytest.raises(_LoopStoppedError):
        module._main_loop(
            inputs=ScheduleInputs(
                Mock(), Mock(), process_context.loaded_code_image(), machine=machine_identity
            ),
            loop_hooks=loop_hooks,
            **({"host": command_host} if filename == "c9-daily-report-schedule.py" else {}),
        )

    assert [call.kwargs["after"] for call in next_fire_call.call_args_list] == [
        now - timedelta(minutes=2),
        start,
    ]
    claim.assert_not_called()
    sleep.assert_called_once_with(60)


@pytest.mark.parametrize("filename", _DAILY_SCRIPTS)
def test_daily_loop_claims_one_due_slot_then_waits_without_retrying(
    filename: str, machine_identity: MachineIdentity
) -> None:
    module = _load_daily_script(filename)
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    now = datetime(2026, 9, 6, 10, 5, tzinfo=UTC)
    slot = datetime(2026, 9, 6, 10, 4, tzinfo=UTC)
    clock = iter((start, now, now))

    claim = Mock(return_value=True)
    sleep = Mock(side_effect=_LoopStoppedError)
    loop_hooks = DailyLoopHooks(
        now_fn=lambda: next(clock),
        next_fire_fn=Mock(return_value=slot),
        claim_fn=claim,
        sleep_fn=sleep,
    )

    command_host = fake_host()
    with pytest.raises(_LoopStoppedError):
        module._main_loop(
            inputs=ScheduleInputs(
                Mock(), Mock(), process_context.loaded_code_image(), machine=machine_identity
            ),
            loop_hooks=loop_hooks,
            **({"host": command_host} if filename == "c9-daily-report-schedule.py" else {}),
        )

    claim.assert_called_once_with(ANY, slot, None, fire=ANY)
    fire = claim.call_args.kwargs["fire"]
    if filename == "c9-daily-report-schedule.py":
        assert isinstance(fire, partial)
        assert fire.func is module._fire
        assert fire.keywords["host"] is command_host
        assert callable(fire.keywords["producer"])
        assert fire.keywords["image"] is process_context.loaded_code_image()
    else:
        assert fire is module._fire
    sleep.assert_called_once_with(120)


@pytest.mark.parametrize(
    ("filename", "entry", "cron"),
    [
        ("self-evolution-daily-schedule.py", "main", "0 9 * * *"),
        ("self-evolution-weekly-schedule.py", "_main_loop", "0 9 * * 1"),
    ],
)
def test_restart_does_not_fire_a_slot_inside_the_previous_two_minutes(
    filename: str,
    entry: str,
    cron: str,
    machine_identity: MachineIdentity,
) -> None:
    module = _load_daily_script(filename)
    started = datetime(2026, 9, 7, 9, 0, 30, tzinfo=UTC)
    claim = Mock()
    sleep = Mock(side_effect=_LoopStoppedError)
    if entry == "main":
        hooks = module.LoopHooks(
            now_fn=lambda: started,
            tz_fn=lambda: "UTC",
            claim_fn=claim,
            sleep_fn=sleep,
            cron=cron,
        )
    else:
        hooks = module.LoopHooks(
            now_fn=lambda: started,
            tz_fn=lambda: "UTC",
            claim_fn=claim,
            sleep_fn=sleep,
            monday_cron=cron,
            thursday_cron="0 9 * * 4",
        )

    with pytest.raises(_LoopStoppedError):
        getattr(module, entry)(
            inputs=ScheduleInputs(
                Mock(), Mock(), process_context.loaded_code_image(), machine=machine_identity
            ),
            hooks=hooks,
        )

    claim.assert_not_called()
    sleep.assert_called_once_with(3600)
