"""Rollout robustness — defects a live 2026-07-28 rollout exposed.

Each of these is a regression test for something the rollout did silently:
- the fan-out dropped a probe-live host because of a stale `machines.stopped_at`,
  and reported a bare count that hid it;
- the roster showed that same host `online` throughout, so the two sources of
  truth disagreed with nothing to see;
- the updater's recovery branch could only fire on a checkout/sync failure, so a
  failed `meiva restart` reached no fallback at all.

No cluster is required: the `machines` reads and the ops probe are stubbed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

import cli.commands._repo as _repo_commands
import cli.commands.lifecycle._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.lifecycle.start as _start_commands
import cli.commands.lifecycle.stop as _stop_commands
from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.telemetry import EventPipeline
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── Defect 1: the fan-out reconciled against a live probe ───────────────────


# ─── Defect 1 (visibility): the roster names the disagreement ────────────────


def test_roster_flags_a_live_host_that_carries_a_stop_marker() -> None:
    """`online` here is what hid the exclusion: the roster's own source of truth
    (a live probe) contradicted the fan-out's (the marker) with nothing to see."""
    from cli.commands.cluster.control import status_cell

    stopped = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"
    # the other three verdicts are unchanged
    assert status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert status_cell(online=True, identity_mismatch=True, stopped_at=stopped) == "MISMATCH"


# ─── Defect 3: declined restart vs failed restart ────────────────────────────


def test_declined_restart_reports_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A preflight refusal stops nothing, so the host is still serving. It must be
    distinguishable from a failure after the stop — the updater shell branches on
    exactly this code to decide whether to run `meiva start`."""

    stopped: list[bool] = []

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 1

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "do_stop", lambda *_a, **_k: stopped.append(True) or 0)  # type: ignore[func-returns-value]

    assert (
        _stop_commands.cmd_restart(
            only_services=(),
            disabled_services=(),
            all_services=True,
            keep_shell_sessions=False,
            keep_browser=True,
            restart_extras=False,
            force=False,
            timeout=30.0,
            start_timeout=30.0,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == RESTART_DECLINED_EXIT_CODE
    )
    assert stopped == []  # validate-before-kill: nothing was taken down


def test_failed_restart_after_the_stop_is_not_reported_as_declined(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Once the stop has happened the host may be DOWN, so its code must NOT be the
    one the updater treats as "still serving"."""

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def start_result(database_factory: Callable[[], Any], **kwargs: object) -> int:
        assert database_factory is operator_database
        assert kwargs["producer"] is operator_pipeline
        return 1

    monkeypatch.setattr(_start_commands, "cmd_start_body", start_result)

    rc = _stop_commands.cmd_restart(
        only_services=(),
        disabled_services=(),
        all_services=True,
        keep_shell_sessions=False,
        keep_browser=True,
        restart_extras=False,
        force=False,
        timeout=30.0,
        start_timeout=30.0,
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc != 0
    assert rc != RESTART_DECLINED_EXIT_CODE


# meiva:note
# Through `meiva restart` itself: a stop leg that fails is the command's result, and
# no compensating start runs.
def test_a_failed_stop_leg_is_reported_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:

    def passes(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", passes)
    monkeypatch.setattr(_start_readiness_preflight_commands, "preflight_start_readiness", passes)

    def failing_stop(*_args: object, **_kwargs: object) -> int:
        return 1

    started: list[bool] = []
    monkeypatch.setattr(_stop_commands, "do_stop", failing_stop)

    def start(*_args: object, **_kwargs: object) -> int:
        started.append(True)
        return 0

    monkeypatch.setattr(_start_commands, "cmd_start_body", start)

    rc = _stop_commands.cmd_restart(
        only_services=(),
        disabled_services=(),
        all_services=True,
        keep_shell_sessions=False,
        keep_browser=True,
        restart_extras=False,
        force=False,
        timeout=30.0,
        start_timeout=30.0,
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert rc == 1
    # meiva:note
    # A failed stop is not compensated with a start.
    assert started == []
