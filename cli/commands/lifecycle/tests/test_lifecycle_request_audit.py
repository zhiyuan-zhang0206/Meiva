from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import cli.commands.lifecycle.root_driver as driver
from base.events.contract import EVENTS
from cli.database.lifecycle_audit import (
    lifecycle_audit,
    operator_request_recorder,
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
