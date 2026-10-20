"""`meiva stop`: the browser flag and the announcement around confirmation and teardown."""

from __future__ import annotations

import subprocess as subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import cli.commands.lifecycle.stop as _stop_commands
from base.cluster.machine import MachineIdentity
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.data_plane import bringup
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    ignore_retained_children as _ignoring_retention,
)
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    restart_command_environment as restart_command_environment,
)
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    root_stop_boundary as root_stop_boundary,
)
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_fakes import (
    FakeResponse,
    patch_gateway_http,
)
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_real_reap_cluster_chrome = _stop_commands.reap_cluster_chrome


# ─── restart ─────────────────────────────────────────────────────────────────


def test_cmd_stop_passes_every_operator_choice_to_do_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # `cmd_stop` forwards each choice unchanged; it adds only the announcement.
    from cli.commands.lifecycle import stop as _stop_mod

    seen: list[dict[str, object]] = []

    def fake_do_stop(_repo: Path, **kwargs: object) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(_stop_mod, "do_stop", fake_do_stop)
    monkeypatch.setattr(_stop_mod, "repo_root", lambda: tmp_path)

    for keep_browser, keep_shell_sessions, teardown_extras, force in (
        (True, True, False, False),
        (False, False, True, True),
    ):
        _stop_mod.cmd_stop(
            keep_infra=None,
            require_confirmation=False,
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            teardown_extras=teardown_extras,
            preserve_sessions=frozenset({"worker"}),
            force=force,
            timeout=7.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        choices = seen[-1]
        assert choices["keep_browser"] is keep_browser
        assert choices["keep_shell_sessions"] is keep_shell_sessions
        assert choices["teardown_extras"] is teardown_extras
        assert choices["force"] is force
        assert choices["keep_infra"] is None
        assert choices["preserve_sessions"] == frozenset({"worker"})
        assert choices["timeout"] == 7.0
        assert choices["announce"] is True


# ─── gateway-backed CLI paths (stop announce) ──────────────────────────────
def _patch_stop_teardown(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, machine: MachineIdentity
) -> None:
    # meiva:note
    # Run force stop on a gateway host with a private root boundary and recorded storage teardown.
    machine.set(role=frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(_stop_commands, "repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        bringup,
        "stop_cluster_instance",
        _ignoring_retention(lambda: events.append("infra") or 0),
    )


def test_cmd_stop_announces_stopping_after_confirm_before_teardown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A confirmed `meiva stop` best-effort POSTs
    /api/cluster/stopping?machine=<self>&home=<self-home> before the local
    teardown (so the cluster view shows 'stopped', not 'offline'). `home`
    identifies THIS unit so a co-located peer keeps its caps."""
    from base.paths import meiva_home

    patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="test-host")
    events: list[str] = []
    calls: list[tuple[str, dict]] = []

    def _fake_post(url, **kwargs):
        events.append("announce")
        calls.append((url, kwargs))  # pyright: ignore[reportUnknownMemberType]
        return FakeResponse({"machine": "test-host"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events, machine=command_runtime.machine)

    rc = _stop_commands.cmd_stop(
        require_confirmation=False,
        keep_infra=False,
        keep_browser=True,
        keep_shell_sessions=False,
        teardown_extras=False,
        preserve_sessions=frozenset(),
        force=True,
        timeout=5.0,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )
    assert rc == 0
    assert calls[0][0] == "http://gw:8000/api/cluster/stopping"
    assert calls[0][1]["params"] == {"machine": "test-host", "home": str(meiva_home())}
    assert events == ["announce", "infra"]  # announced first, then teardown ran


def test_cmd_stop_aborted_confirm_does_not_announce(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Declining the confirm prompt must leave the roster untouched: the stopping
    announce stamps `machines.stopped_at`, and only the next `meiva start` clears
    it — an announce fired before the gate would mark a running host 'stopped'."""
    patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="test-host")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: events.append("announce"))  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events, machine=command_runtime.machine)

    rc = _stop_commands.cmd_stop(
        require_confirmation=True,
        keep_infra=False,
        keep_browser=True,
        keep_shell_sessions=False,
        teardown_extras=False,
        preserve_sessions=frozenset(),
        force=True,
        timeout=5.0,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )
    assert rc == 0
    assert events == []  # no announce, no teardown
    assert "aborted" in capsys.readouterr().out


def test_cmd_stop_proceeds_when_announce_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """If the stopping announce can't reach the gateway, `meiva stop` logs and still
    tears down — the announce is best-effort, never a blocker."""
    patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="runner-a")

    def _boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.post", _boom)  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    _patch_stop_teardown(monkeypatch, events, machine=command_runtime.machine)

    rc = _stop_commands.cmd_stop(
        require_confirmation=False,
        keep_infra=False,
        keep_browser=True,
        keep_shell_sessions=False,
        teardown_extras=False,
        preserve_sessions=frozenset(),
        force=True,
        timeout=5.0,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )
    assert rc == 0
    assert events == ["infra"]  # teardown still ran
    assert "could not announce shutdown" in capsys.readouterr().out
