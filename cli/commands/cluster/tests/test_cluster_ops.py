# meiva:note
# The reading surface for lifecycle operations (task #6024). The rows these tests feed are the
# shape `audit_events` really holds: a caller block, a spawn block, and — since the fleet window
# mints them — an operation id, an initiator and a gate verdict per row.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cli.commands.cluster.ops import cmd_cluster_ops


class _Cursor:
    def __init__(self, rows: list[tuple[datetime, str, str]]) -> None:
        self._rows = rows

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object) -> None:
        del sql, params

    def fetchall(self) -> list[tuple[datetime, str, str]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[tuple[datetime, str, str]]) -> None:
        self._rows = rows

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return _Cursor(self._rows)


class _Database:
    def __init__(self, rows: list[tuple[datetime, str, str]]) -> None:
        self._rows = rows

    def connect(self, *, autocommit: bool = False) -> _Connection:
        assert autocommit, "the reader never opens a transaction it does not commit"
        return _Connection(self._rows)


def _row(
    *,
    ts: datetime,
    event: str,
    verb: str,
    machine: str,
    operation_id: str | None,
    initiator: str | None = None,
    gate: str | None = None,
    gate_reason: str | None = None,
    outcome: str | None = None,
) -> tuple[datetime, str, str]:
    attributes: dict[str, Any] = {
        "verb": verb,
        "caller": {"machine": machine, "agent_id": None},
        "spawn": {"ssh_connection": "100.0.0.1 1 100.0.0.2 22"},
        "operation_id": operation_id,
        "initiator": initiator,
        "gate": gate,
        "gate_reason": gate_reason,
    }
    if outcome is not None:
        attributes["outcome"] = outcome
    return ts, event, json.dumps(attributes)


def _factory(rows: list[tuple[datetime, str, str]]) -> Any:
    return lambda: _Database(rows)


_T0 = datetime(2026, 11, 2, 9, 19, 55, tzinfo=UTC)


def test_one_window_is_one_line_naming_who_started_it_and_what_the_gate_said(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(
            ts=_T0,
            event="lifecycle_request",
            verb="down",
            machine="macmini",
            operation_id="op-a",
            initiator="user:zy@laptop",
            gate="satisfied",
            gate_reason="channel",
        ),
        _row(
            ts=_T0 + timedelta(seconds=5),
            event="lifecycle_request",
            verb="down",
            machine="ubuntu",
            operation_id="op-a",
            initiator="user:zy@laptop",
            gate="satisfied",
            gate_reason="channel",
        ),
        _row(
            ts=_T0 + timedelta(seconds=9),
            event="lifecycle_result",
            verb="down",
            machine="ubuntu",
            operation_id="op-a",
            outcome="ok",
        ),
    ]
    assert cmd_cluster_ops(database_factory=_factory(rows)) == 0
    out = capsys.readouterr().out
    assert "op-a" in out
    assert "user:zy@laptop" in out
    assert "satisfied (channel)" in out
    assert "2 host(s)" in out


def test_a_bypassed_gate_reads_as_overridden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(
            ts=_T0,
            event="lifecycle_request",
            verb="down",
            machine="macmini",
            operation_id="op-b",
            initiator="user:zy@laptop",
            gate="overridden",
            gate_reason="track-branch:preview",
        )
    ]
    assert cmd_cluster_ops(database_factory=_factory(rows)) == 0
    assert "overridden (track-branch:preview)" in capsys.readouterr().out


def test_rows_without_an_operation_id_stay_separate_operations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(ts=_T0, event="lifecycle_request", verb="down", machine="macmini", operation_id=None),
        _row(
            ts=_T0 + timedelta(minutes=3),
            event="lifecycle_request",
            verb="down",
            machine="ubuntu",
            operation_id=None,
        ),
    ]
    assert cmd_cluster_ops(database_factory=_factory(rows)) == 0
    out = capsys.readouterr().out
    # meiva:note
    # Two commands an operator typed are two operations; folding them into one would invent a
    # window nobody ran.
    assert out.count("1 host(s)") == 2


def test_expanding_one_operation_names_each_host(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(
            ts=_T0,
            event="lifecycle_request",
            verb="up",
            machine="macmini",
            operation_id="op-c",
            initiator="user:zy@laptop",
            gate="satisfied",
            gate_reason="release",
        ),
        _row(
            ts=_T0 + timedelta(seconds=2),
            event="lifecycle_request",
            verb="up",
            machine="ubuntu",
            operation_id="op-c",
            initiator="user:zy@laptop",
            gate="satisfied",
            gate_reason="release",
        ),
    ]
    assert cmd_cluster_ops(database_factory=_factory(rows), operation="op-c") == 0
    out = capsys.readouterr().out
    assert "macmini" in out and "ubuntu" in out


def test_an_unknown_operation_is_reported_rather_than_answered_with_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(
            ts=_T0,
            event="lifecycle_request",
            verb="down",
            machine="macmini",
            operation_id="op-d",
        )
    ]
    assert cmd_cluster_ops(database_factory=_factory(rows), operation="op-missing") == 1
    assert "op-missing" in capsys.readouterr().out


def test_an_empty_window_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_cluster_ops(database_factory=_factory([])) == 0
    assert "no lifecycle operations" in capsys.readouterr().out
