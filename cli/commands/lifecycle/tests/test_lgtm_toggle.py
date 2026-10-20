from collections.abc import Callable
from pathlib import Path

import pytest

import cli.commands.lifecycle.start as lifecycle_start
import services.supervision.meiva_root_glue.manifests as root_manifests
from base.telemetry import EventPipeline
from cli.commands.lifecycle import lgtm_toggle as lgtm
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.supervision import roster
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _launched(names: frozenset[str] | None) -> Callable[[Path], frozenset[str] | None]:
    # meiva:note
    # A `launched_services` reader that reports `names` for any manifest path.

    def read(_path: Path) -> frozenset[str] | None:
        return names

    return read


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launched: frozenset[str] | None
) -> tuple[Path, list[dict[str, object]]]:
    marker = tmp_path / "lgtm-host"
    monkeypatch.setattr(lgtm, "lgtm_host_marker", lambda: marker)
    monkeypatch.setattr(root_manifests, "launched_services", _launched(launched))
    calls: list[dict[str, object]] = []

    def start(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(lifecycle_start, "cmd_start", start)
    return marker, calls


def test_on_preserves_explicit_other_service_choices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, frozenset({"ops"}))
    assert (
        lgtm.cmd_lgtm_on(
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
            "only_services": ("grafana", "loki", "ops", "prometheus"),
            "disabled_services": (),
            "all_services": False,
            "retained_children": [],
            "database_factory": operator_database,
            "producer": operator_pipeline,
            "command": command_runtime,
        }
    ]


def test_off_disables_backends_even_on_role_declared_station(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, frozenset({"ops", "browser", *lgtm.BACKENDS}))
    marker.touch()
    data = tmp_path / "loki-data"
    data.write_bytes(b"durable history")
    assert (
        lgtm.cmd_lgtm_off(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert not marker.exists()
    assert calls == [
        {
            "only_services": ("browser", "ops"),
            "disabled_services": (),
            "all_services": False,
            "retained_children": [],
            "database_factory": operator_database,
            "producer": operator_pipeline,
            "command": command_runtime,
        }
    ]
    assert data.read_bytes() == b"durable history"


def test_off_only_backend_allowlist_does_not_enable_all_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from types import SimpleNamespace

    _marker, calls = _wire(monkeypatch, tmp_path, frozenset(lgtm.BACKENDS))
    monkeypatch.setattr(
        roster,
        "build_services",
        lambda: tuple(
            SimpleNamespace(session=n) for n in ("gateway", "loki", "prometheus", "grafana")
        ),
    )
    assert (
        lgtm.cmd_lgtm_off(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert calls == [
        {
            "only_services": (),
            "disabled_services": ("gateway", "loki", "prometheus", "grafana"),
            "all_services": False,
            "retained_children": [],
            "database_factory": operator_database,
            "producer": operator_pipeline,
            "command": command_runtime,
        }
    ]


def test_normal_start_refusal_is_not_reported_as_toggle_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    _wire(monkeypatch, tmp_path, frozenset({"ops"}))

    def refuse(**_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(lifecycle_start, "cmd_start", refuse)
    assert (
        lgtm.cmd_lgtm_on(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 1
    )


def test_a_unit_no_start_launched_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # With no launch manifest there is no selection to change: name one with `meiva start`.
    _marker, calls = _wire(monkeypatch, tmp_path, None)
    with pytest.raises(RuntimeError, match="explicit service selection"):
        lgtm.cmd_lgtm_on(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
    assert calls == []
