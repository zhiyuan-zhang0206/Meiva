from collections.abc import Callable
from pathlib import Path

import pytest

import cli.commands.lifecycle.start as lifecycle_start
from base.telemetry import EventPipeline
from cli.commands.lifecycle import lgtm_toggle as lgtm
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

BACKENDS = tuple(sorted(lgtm.BACKENDS))


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rc: int = 0
) -> tuple[Path, list[dict[str, object]]]:
    marker = tmp_path / "lgtm-host"
    monkeypatch.setattr(lgtm, "lgtm_host_marker", lambda: marker)
    calls: list[dict[str, object]] = []

    def start(**kwargs: object) -> int:
        calls.append(kwargs)
        return rc

    monkeypatch.setattr(lifecycle_start, "cmd_start", start)
    return marker, calls


@pytest.mark.parametrize(
    ("only", "disabled", "every"),
    [((), (), True), (("ops", *BACKENDS), (), False), ((), ("browser",), False)],
)
def test_on_starts_exactly_the_operators_selection(
    only: tuple[str, ...],
    disabled: tuple[str, ...],
    every: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    assert (
        lgtm.cmd_lgtm_on(
            only_services=only,
            disabled_services=disabled,
            all_services=every,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert marker.exists()
    assert calls == [
        {
            "only_services": only,
            "disabled_services": disabled,
            "all_services": every,
            "retained_children": [],
            "database_factory": operator_database,
            "producer": operator_pipeline,
            "command": command_runtime,
        }
    ]


def test_off_retracts_the_station_and_keeps_its_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    marker.touch()
    data = tmp_path / "loki-data"
    data.write_bytes(b"durable history")
    assert (
        lgtm.cmd_lgtm_off(
            only_services=(),
            disabled_services=BACKENDS,
            all_services=False,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert not marker.exists()
    assert [call["disabled_services"] for call in calls] == [BACKENDS]
    assert data.read_bytes() == b"durable history"


@pytest.mark.parametrize(
    ("enabled", "only", "disabled", "message"),
    [
        (True, (), (BACKENDS[0],), "cannot --disable-service"),
        (True, ("ops",), (), "must name every LGTM backend"),
        (False, (BACKENDS[0],), (), "cannot --only-service"),
    ],
)
def test_a_selection_that_contradicts_the_toggle_is_refused_before_any_change(
    enabled: bool,
    only: tuple[str, ...],
    disabled: tuple[str, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path)
    if not enabled:
        marker.touch()
    toggle = lgtm.cmd_lgtm_on if enabled else lgtm.cmd_lgtm_off
    with pytest.raises(ValueError, match=message):
        toggle(
            only_services=only,
            disabled_services=disabled,
            all_services=False,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
    assert calls == []
    assert marker.exists() is not enabled


def test_normal_start_refusal_is_not_reported_as_toggle_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    _wire(monkeypatch, tmp_path, rc=1)
    assert (
        lgtm.cmd_lgtm_on(
            only_services=(),
            disabled_services=(),
            all_services=True,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 1
    )
