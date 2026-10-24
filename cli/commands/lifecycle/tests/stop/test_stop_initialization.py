# meiva:note
# A stop of a home that never admitted work closes the attempt and drains nothing.
#
# Positive first-start evidence (an identity journal still in its `configured` phase, no serving
# authority, no launched manifest, no root, no shell session) lets the stop use the native owners
# alone; any application evidence sends it through the ordinary drain instead.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base import paths
from base.deploy.lifecycle import start_serving
from base.native_process.root_control.instance_lock import (
    acquire_instance_lock,
    release_instance_lock,
)
from base.telemetry import EventPipeline
from cli.commands.lifecycle import stand_down
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.stop.evidence import require_unstarted_initialization
from cli.commands.lifecycle.tests.stop.stop_support import (
    home as home,
)
from cli.commands.lifecycle.tests.stop.stop_support import (
    stop_environment,
    stop_request,
)
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.start_identity import IdentityInput, mark_phase, prepare_identity


@pytest.fixture
def partial_home(home: Path) -> Path:
    """A home that was initialized as a gateway and never started."""
    checkout = home / "checkout"
    checkout.mkdir()
    prepare_identity(
        IdentityInput(
            home,
            checkout,
            frozenset({"gateway", "agent-runner"}),
            {"MEIVA_MACHINE_NAME": "partial"},
            occupied_ports=frozenset(),
        )
    )
    return home


def test_configured_without_application_evidence_is_proven_unstarted(partial_home: Path) -> None:
    assert require_unstarted_initialization(partial_home) is True


def test_a_home_with_no_identity_journal_is_not_proven_unstarted(home: Path) -> None:
    assert require_unstarted_initialization(home) is False


@pytest.mark.parametrize("phase", ["provisioned", "ready"])
def test_progressed_start_must_use_ordinary_drain(partial_home: Path, phase: str) -> None:
    mark_phase(partial_home, phase)
    assert require_unstarted_initialization(partial_home) is False


@pytest.mark.parametrize("evidence", ["serving", "manifest", "locked-root"])
def test_any_application_evidence_refuses_partial_shortcut(
    partial_home: Path, evidence: str
) -> None:
    fd = None
    if evidence == "serving":
        start_serving.begin_start(partial_home)
    elif evidence == "manifest":
        path = paths.root_manifests_path(partial_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    else:
        fd = acquire_instance_lock(paths.root_run_dir(partial_home))
    try:
        with pytest.raises(RuntimeError):
            require_unstarted_initialization(partial_home)
    finally:
        if fd is not None:
            release_instance_lock(fd)


# meiva:note
# The close of an attempt that never admitted work has nothing to announce to the
# gateway and no stand-down to leave: it neither revokes serving nor records a stop.
def test_partial_stop_uses_native_cleanup_without_announcing_or_draining(
    partial_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    stop_runtime.machine.set(role="gateway")
    request = stop_request(
        keep_infra=True, keep_browser=False, announce=True, teardown_extras=True, timeout=3
    )
    rc = do_stop(
        request,
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert stand_down.read(partial_home) is None, "no stop was declared"
    phases = capsys.readouterr().out
    assert "stop phase: services" in phases and "stop phase: extras" in phases
    assert "stop phase: shell-sessions" not in phases


def test_a_stop_that_progressed_past_initialization_declares_its_stand_down(
    partial_home: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    mark_phase(partial_home, "ready")
    stop_runtime.machine.set(role="gateway")
    request = stop_request(keep_infra=True, announce=True, timeout=3)
    rc = do_stop(
        request,
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert stand_down.read(partial_home) is not None


def test_contradicting_evidence_fails_the_stop_before_it_confirms_or_stops_anything(
    partial_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    stop_runtime.machine.set(role="gateway")
    start_serving.begin_start(partial_home)
    rc = do_stop(
        stop_request(keep_infra=True, timeout=3),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Stop incomplete" in err, "reported like any incomplete stop, not as a traceback"
    assert "initialization journal conflicts with application launch evidence" in err
    assert "phases: before the first phase" in err
    assert "Status journal" not in err, "there is no journal to point at"
    assert start_serving.state_path().exists(), "nothing was revoked"
    assert not (partial_home / "run/lifecycle-op.json").exists(), "no journal was opened"
