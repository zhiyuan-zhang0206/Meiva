from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import cli.commands.lifecycle.root_driver as driver
from base.events.contract import EVENTS
from cli.database.lifecycle_audit import (
    lifecycle_audit,
    operator_lifecycle_audit,
    operator_request_recorder,
    operator_result_recorder,
    record_lifecycle_request,
)
from services.supervision.roster.service_spec import ServiceSpec


def _caller() -> dict[str, Any]:
    return {
        "pid": 4821,
        "ppid": 4800,
        "uid": 501,
        "cwd": "/src",
        "argv": ["meiva", "stop", "-y", "--token", "<redacted>"],
        "agent_id": "1818",
        "machine": "macmini",
        "operation_id": "stop@1792322999",
        "env_keys": ["MEIVA_API_TOKEN"],
    }


class _Recorder:
    def __init__(self) -> None:
        self.batches: list[tuple[str, list[str]]] = []
        self.events: list[Any] = []

    def __call__(self, verb: str, units: Any) -> None:
        self.batches.append((verb, list(units)))

    def audit(self, _database: Any, event: Any) -> None:
        self.events.append(event)


def _recorder_for(caller: Mapping[str, Any] | None = None) -> tuple[_Recorder, Any]:
    capture = _Recorder()
    recorder = operator_request_recorder(
        database_factory=lambda: "db",
        caller_identity=lambda: caller if caller is not None else _caller(),
        record_audit_reported=capture.audit,
    )
    return capture, recorder


def _spec(session: str) -> ServiceSpec:
    return ServiceSpec(
        session=session,
        cmd="python -m service",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        identity_probe=None,
    )


def _status(state: str, *sessions: str) -> dict[str, Any]:
    return {
        "root": {"pid": 90},
        "units": [{"id": name, "state": state, "intent": "running"} for name in sessions],
    }


class _UnitClient:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.up_calls: list[str] = []

    def up(self, unit_id: str) -> dict[str, Any]:
        self.up_calls.append(unit_id)
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "result": self.snapshot}


def test_the_event_is_registered_as_audit() -> None:
    assert EVENTS["lifecycle_request"].category == "audit"
    assert EVENTS["lifecycle_result"].category == "audit"


def _result_recorder_for(capture: _Recorder, head: str | None = "ce79dbbb") -> Any:
    return operator_result_recorder(
        database_factory=lambda: "db",
        caller_identity=_caller,
        record_audit_reported=capture.audit,
        head_sha=lambda: head,
    )


def test_the_request_and_the_result_name_the_same_version(tmp_path: Any) -> None:
    capture = _Recorder()
    request = operator_request_recorder(
        database_factory=lambda: "db",
        caller_identity=_caller,
        record_audit_reported=capture.audit,
        head_sha=lambda: "39d4614c1",
    )
    result = _result_recorder_for(capture, head="39d4614c1")

    with lifecycle_audit(request, result) as command:
        record_lifecycle_request("down", ["gateway", "agent-host"])
        command.exited(0)

    request_event, result_event = capture.events
    assert request_event.attributes["head_sha"] == "39d4614c1"
    assert result_event.event_name == "lifecycle_result"
    assert result_event.attributes["verb"] == "down"
    assert result_event.attributes["units"] == ["gateway", "agent-host"]
    assert result_event.attributes["outcome"] == "ok"
    assert result_event.attributes["head_sha"] == "39d4614c1"
    assert result_event.attributes["seconds"] >= 0


def test_a_failed_command_records_a_failed_outcome() -> None:
    capture = _Recorder()
    with lifecycle_audit(None, _result_recorder_for(capture)) as command:
        record_lifecycle_request("up", ["gateway"])
        command.exited(1)

    assert capture.events[0].attributes["outcome"] == "failed"


def test_a_command_without_a_request_records_no_result() -> None:
    capture = _Recorder()

    with lifecycle_audit(None, _result_recorder_for(capture)) as command:
        command.exited(0)

    assert capture.events == []


def test_a_boot_start_is_not_attributed_to_an_operator() -> None:
    capture = _Recorder()
    recorded: list[tuple[str, list[str]]] = []

    def refuse() -> Any:
        raise AssertionError("a boot start must not reach the database")

    with operator_lifecycle_audit(database_factory=refuse, enabled=False) as command:
        record_lifecycle_request("up", ["gateway"])
        command.exited(0)

    assert capture.events == []
    assert recorded == []


def test_the_outcome_is_recorded_once() -> None:
    raised = False
    with lifecycle_audit(None) as command:
        command.exited(0)
        try:
            command.exited(0)
        except RuntimeError:
            raised = True
    assert raised


def test_a_command_scope_records_and_restores_the_previous_recorder() -> None:
    outer, inner = _Recorder(), _Recorder()

    with lifecycle_audit(outer):
        with lifecycle_audit(inner):
            record_lifecycle_request("down", ["gateway"])
        record_lifecycle_request("up", ["gateway"])
    record_lifecycle_request("down", ["gateway"])

    assert inner.batches == [("down", ["gateway"])]
    assert outer.batches == [("up", ["gateway"])]


def test_outside_a_command_no_request_is_recorded() -> None:
    capture = _Recorder()

    with lifecycle_audit(None):
        record_lifecycle_request("down", ["gateway"])

    assert capture.batches == []


def test_the_record_carries_the_wire_caller_block() -> None:
    capture, recorder = _recorder_for()

    recorder("down", ["gateway", "frontend"])

    event = capture.events[0]
    assert event.category == "audit"
    assert event.event_name == "lifecycle_request"
    assert event.source == "agent:1818"
    assert event.agent_id == 1818
    assert event.attributes["verb"] == "down"
    assert event.attributes["units"] == ["gateway", "frontend"]
    assert event.attributes["operation_id"] == "stop@1792322999"
    assert event.attributes["caller"]["machine"] == "macmini"
    assert event.attributes["caller"]["argv"][-1] == "<redacted>"


def test_a_human_operator_is_recorded_without_an_agent_id() -> None:
    caller = _caller()
    caller["agent_id"] = None
    capture, recorder = _recorder_for(caller)

    recorder("up", ["gateway"])

    assert capture.events[0].source == "user"
    assert capture.events[0].agent_id is None


def test_an_unreachable_database_never_fails_the_command() -> None:

    def refuse() -> Any:
        raise RuntimeError("settings unavailable")

    recorder = operator_request_recorder(
        database_factory=refuse,
        caller_identity=_caller,
        record_audit_reported=_Recorder().audit,
    )

    recorder("down", ["gateway"])


def test_the_driver_records_the_units_before_it_asks() -> None:
    capture, recorder = _recorder_for()
    client = _UnitClient(_status("running", "gateway"))

    with lifecycle_audit(recorder):
        driver.reconcile_units((_spec("gateway"),), client, _status("exited", "gateway"))

    assert capture.events[0].attributes["verb"] == "up"
    assert capture.events[0].attributes["units"] == ["gateway"]
    assert client.up_calls == ["gateway"]
