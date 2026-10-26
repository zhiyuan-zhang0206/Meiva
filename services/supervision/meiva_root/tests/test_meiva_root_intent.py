from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL, ProcessControl
from base.native_process.tests.fake_process_control import FakeProcessControl
from services.supervision.meiva_root import intent_store
from services.supervision.meiva_root.inputs import InputSeal
from services.supervision.meiva_root.intent_store import (
    CallerFacts,
    IntentRecord,
    IntentSource,
    RestartFailure,
    RestartStage,
    UnitIntent,
    merge_record_for_boot,
)
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.meiva_root.tests.slices import system_clock

_CHILD = Path(__file__).with_name("child_unit.py")
_SLEEP_FOREVER = ("sleep",)


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [{"message": r["message"], **r["extra"]} for r in self._records]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


def _unit(
    *scenario: str,
    unit_id: str = "svc",
    inputs: tuple[InputSeal, ...] = (),
) -> UnitManifest:
    return UnitManifest(
        unit_id,
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
        inputs=inputs,
    )


def _supervisor(
    run_dir: Path,
    units: list[UnitManifest],
    *,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
) -> Supervisor:
    return Supervisor(
        UnitRegistry(units),
        run_dir=run_dir,
        config=SupervisorConfig(stop_timeout_s=0.2),
        process_control=process_control,
        clock=system_clock(),
        root_pid=os.getpid(),
    )


def _sealed_input(tmp_path: Path, unit_id: str = "svc") -> tuple[Path, InputSeal]:
    path = tmp_path / f"{unit_id}-input"
    path.write_text("v1")
    path = path.resolve()
    return path, InputSeal.capture(path)


async def _entry(owner: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]


async def _wait_state(owner: Supervisor, want: str) -> None:
    for _ in range(250):
        if (await _entry(owner))["state"] == want:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"unit did not reach state {want!r}")


async def _wait_file(path: Path) -> None:
    for _ in range(250):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"file {path} never appeared")


def test_boot_merge_rules() -> None:
    merged = merge_record_for_boot(None)
    assert (merged.intent, merged.source, merged.restart_failed, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.SELECTION,
        None,
        True,
    )
    failure = RestartFailure(RestartStage.UP, 1234.5, "boom")
    merged = merge_record_for_boot(
        IntentRecord(UnitIntent.STOPPED, IntentSource.OPERATOR, None, updated_at=1.0)
    )
    assert (merged.intent, merged.source, merged.start) == (
        UnitIntent.STOPPED,
        IntentSource.OPERATOR,
        False,
    )
    assert merged.note is not None
    merged = merge_record_for_boot(
        IntentRecord(UnitIntent.STOPPED, IntentSource.SELECTION, None, updated_at=1.0)
    )
    assert (merged.intent, merged.start) == (UnitIntent.STOPPED, False)
    merged = merge_record_for_boot(
        IntentRecord(UnitIntent.STOPPED, IntentSource.SELF, None, updated_at=1.0)
    )
    assert (merged.intent, merged.source, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.SELECTION,
        True,
    )
    merged = merge_record_for_boot(
        IntentRecord(UnitIntent.RUNNING, IntentSource.OPERATOR, failure, updated_at=1.0)
    )
    assert (merged.intent, merged.source, merged.restart_failed, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.OPERATOR,
        failure,
        True,
    )


def test_record_round_trip_and_corruption(tmp_path: Path) -> None:
    assert intent_store.read(tmp_path, "svc") is None
    record = IntentRecord(
        UnitIntent.RUNNING,
        IntentSource.OPERATOR,
        RestartFailure(RestartStage.DOWN, 99.5, "stop refused"),
        updated_at=1.0,
    )
    intent_store.write(tmp_path, "svc", record)
    _assert_same_record(intent_store.read(tmp_path, "svc"), record)
    (tmp_path / "intent" / "svc.json").write_text("{not json")
    assert intent_store.read(tmp_path, "svc") is None
    (tmp_path / "intent" / "svc.json").write_text(
        '{"intent": "sideways", "source": "operator", "restart_failed": null, "updated_at": 1}'
    )
    assert intent_store.read(tmp_path, "svc") is None


async def test_boot_holds_an_explicit_stored_stop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="services.supervision.meiva_root.supervisor")
    intent_store.write(
        tmp_path,
        "svc",
        IntentRecord(UnitIntent.STOPPED, IntentSource.OPERATOR, None, updated_at=1.0),
    )
    owner = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER)])
    await owner.start()
    try:
        entry = await _entry(owner)
        assert entry["intent"] == "stopped"
        assert entry["intent_source"] == "operator"
        assert entry["state"] == "stopped" and entry["pid"] is None
        assert owner.revival_deferral("svc") == "held down"
        assert "stored operator stop held" in caplog.text
        await owner.up("svc")
        entry = await _entry(owner)
        assert entry["intent"] == "running" and entry["state"] == "running"
    finally:
        await owner.shutdown()


async def test_boot_holds_a_stored_selection_stop(tmp_path: Path) -> None:
    intent_store.write(
        tmp_path,
        "svc",
        IntentRecord(UnitIntent.STOPPED, IntentSource.SELECTION, None, updated_at=1.0),
    )
    owner = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER)])
    await owner.start()
    try:
        entry = await _entry(owner)
        assert (entry["intent"], entry["intent_source"], entry["state"]) == (
            "stopped",
            "selection",
            "stopped",
        )
        assert owner.revival_deferral("svc") == "held down"
    finally:
        await owner.shutdown()


async def test_boot_supersedes_a_stored_root_stop(tmp_path: Path) -> None:
    first = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER)])
    await first.start()
    await first.shutdown()
    stored = intent_store.read(tmp_path, "svc")
    assert stored is not None and stored.source is IntentSource.SELF

    second = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER)])
    await second.start()
    try:
        entry = await _entry(second)
        assert (entry["intent"], entry["state"]) == ("running", "running")
        _assert_same_record(
            intent_store.read(tmp_path, "svc"),
            IntentRecord(UnitIntent.RUNNING, IntentSource.SELECTION, None, updated_at=1.0),
        )
    finally:
        await second.shutdown()


async def test_restart_up_half_failure_records_explicit_state(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    owner = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER, inputs=(seal,))])
    await owner.start()
    try:
        input_path.write_text("v2")
        result = await owner.restart("svc")
        entry = cast("list[dict[str, Any]]", result["units"])[0]
        assert entry["action"] == "failed"
        status = await _entry(owner)
        assert status["intent"] == "running" and status["state"] == "stopped"
        assert status["restart_failed"]["stage"] == "up"
        assert "input" in status["restart_failed"]["detail"]
        assert owner.revival_deferral("svc") is None, "a failed restart is not an operator stop"
        stored = intent_store.read(tmp_path, "svc")
        assert stored is not None and stored.intent is UnitIntent.RUNNING
        assert stored.restart_failed is not None and stored.restart_failed.stage is RestartStage.UP
        assert len(recorder.events("root_restart_failed")) == 1
        assert recorder.events("root_restart_cleared") == []
    finally:
        await owner.shutdown()


async def test_restart_up_half_failure_retry_replaces_and_clears(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    pc = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    owner = _supervisor(
        tmp_path,
        [_unit(*_SLEEP_FOREVER, inputs=(seal,))],
        process_control=pc,
    )
    await owner.start()
    original = owner._units["svc"].generation
    assert original is not None
    try:
        input_path.write_text("v2")
        await owner.restart("svc")
        assert original.exited.is_set() and original.proc.returncode is not None
        failed = intent_store.read(tmp_path, "svc")
        assert failed is not None and failed.intent is UnitIntent.RUNNING
        assert failed.restart_failed is not None and failed.restart_failed.stage is RestartStage.UP
        pc.signals.clear()
        input_path.write_text("v1")
        result = await owner.restart("svc")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "restarted"
        status = await _entry(owner)
        assert status["restart_failed"] is None
        assert status["state"] == "running"
        replacement = owner._units["svc"].generation
        assert replacement is not None and replacement is not original
        assert replacement.identity is not None and replacement.identity.live()
        assert owner._units["svc"].restart_count == 1
        _assert_same_record(
            intent_store.read(tmp_path, "svc"),
            IntentRecord(UnitIntent.RUNNING, IntentSource.SELECTION, None, updated_at=1.0),
        )
        assert pc.signals == []
        assert len(recorder.events("root_restart_failed")) == 1
        assert len(recorder.events("root_restart_cleared")) == 1
    finally:
        await owner.shutdown()


async def test_restart_down_abort_records_stage_down_and_retry_settles(
    tmp_path: Path, recorder: _Recorder
) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    unit = _unit("restart_down", str(ready), str(release))

    owner = Supervisor(
        UnitRegistry([unit]),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.4, stop_kill_reap_s=0.1),
        process_control=FakeProcessControl(),
        clock=system_clock(),
        root_pid=os.getpid(),
    )
    await owner.start()
    try:
        await _wait_file(ready)
        active = await _entry(owner)
        assert active["state"] == "running"
        with pytest.raises(RuntimeError, match=r"identity changed, so root did not kill it"):
            await owner.restart("svc")
        aborted = recorder.events("root_unit_stop_aborted")
        assert [event["reason"] for event in aborted] == ["identity_changed"]
        status = await _entry(owner)
        assert status["intent"] == "running"
        assert status["state"] == "running", "a refused stop keeps the old generation"
        assert status["pid"] == active["pid"]
        assert status["restart_failed"]["stage"] == "down"
        assert owner.revival_deferral("svc") is None, "retryable, not an operator stop"
        release.touch()
        await _wait_state(owner, "stopped")
        result = await owner.restart("svc")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "restarted"
        status = await _entry(owner)
        assert (status["restart_failed"], status["state"]) == (None, "running")
    finally:
        owner._process_control = SYSTEM_PROCESS_CONTROL
        await owner.shutdown()


async def test_boot_carries_recorded_failure_until_a_generation_proves_it_gone(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    unit = _unit(*_SLEEP_FOREVER, inputs=(seal,))
    first = _supervisor(tmp_path, [unit])
    await first.start()
    input_path.write_text("v2")
    await first.restart("svc")
    stored = intent_store.read(tmp_path, "svc")
    assert stored is not None and stored.restart_failed is not None
    since = stored.restart_failed.since
    second = _supervisor(tmp_path, [unit])
    await second.start()
    try:
        entry = await _entry(second)
        assert entry["intent"] == "running" and entry["state"] == "stopped"
        assert entry["restart_failed"]["stage"] == "up"
        assert entry["restart_failed"]["since"] == since, "the episode keeps its start time"
        assert "spawn failed" in (entry["last_error"] or "")
        assert len(recorder.events("root_restart_failed")) == 1, "no duplicate enter on carry"
        assert recorder.events("root_restart_cleared") == []
        input_path.write_text("v1")
        await second.restart("svc")
        entry = await _entry(second)
        assert entry["restart_failed"] is None and entry["state"] == "running"
        assert len(recorder.events("root_restart_cleared")) == 1
        _assert_same_record(
            intent_store.read(tmp_path, "svc"),
            IntentRecord(UnitIntent.RUNNING, IntentSource.SELECTION, None, updated_at=1.0),
        )
    finally:
        await second.shutdown()


def _caller() -> CallerFacts:
    return CallerFacts(
        pid=4821,
        ppid=4800,
        uid=501,
        cwd="/srv/meiva",
        argv=("meiva", "stop", "-y"),
        agent_id="1818",
        machine="macmini",
        operation_id="local-pause:ubuntu:3696811:a00d0511",
        env_keys=("MEIVA_API_TOKEN",),
    )


# meiva:note
# Compare everything a record says except its wall-clock write time.
def _assert_same_record(actual: IntentRecord | None, expected: IntentRecord) -> None:
    assert actual is not None
    assert (actual.intent, actual.source, actual.restart_failed, actual.caller) == (
        expected.intent,
        expected.source,
        expected.restart_failed,
        expected.caller,
    )
    assert actual.updated_at > 0


def test_a_caller_round_trips_and_a_legacy_record_still_reads(tmp_path: Path) -> None:
    record = IntentRecord(
        UnitIntent.STOPPED, IntentSource.OPERATOR, None, _caller(), updated_at=1.0
    )
    intent_store.write(tmp_path, "svc", record)
    _assert_same_record(intent_store.read(tmp_path, "svc"), record)

    path = tmp_path / "intent" / "svc.json"
    path.write_text(
        '{"intent": "stopped", "source": "operator", "restart_failed": null, "updated_at": 1}'
    )
    legacy = intent_store.read(tmp_path, "svc")
    assert legacy is not None and legacy.caller is None

    intent_store.write(
        tmp_path,
        "svc",
        IntentRecord(UnitIntent.STOPPED, IntentSource.OPERATOR, None, updated_at=1.0),
    )
    assert "caller" not in path.read_text()


async def test_the_stop_that_ends_the_tree_keeps_the_caller(tmp_path: Path) -> None:
    owner = _supervisor(tmp_path, [_unit(*_SLEEP_FOREVER)])
    await owner.start()
    try:
        caller = _caller()
        await owner.down("svc", caller=caller)
        recorded = intent_store.read(tmp_path, "svc")
        assert recorded is not None and recorded.caller == caller
    finally:
        await owner.shutdown()

    stored = intent_store.read(tmp_path, "svc")
    assert stored is not None
    assert stored.intent is UnitIntent.STOPPED
    assert stored.caller == caller
