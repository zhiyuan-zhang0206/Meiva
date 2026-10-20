# meiva:note
# Every stop and restart choice is the operator's: which phases run follows only from them.

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from base.sessions.pty.paths import SERVICE_UNIT
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle.root_driver import RootDriverError
from cli.commands.lifecycle.tests.stop.stop_support import dependencies, record_root_stops
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import lifecycle_audit
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# meiva:note
# ── the phases a stop enters follow from the operator's choices ──────────────


class _Effects:
    # meiva:note
    # What a normal stop actually did: the effects it reached, in the order it reached them.

    def __init__(self) -> None:
        self.done: list[str] = []
        self.root_stops: list[dict[str, Any]] = []

    def pty_sessions_stopped(self) -> bool:
        # meiva:note
        # The root owner was asked to stop the pty-sessions service itself.
        return any(SERVICE_UNIT not in call["preserve"] for call in self.root_stops)


def _stop_effects(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    roles: str,
    keep_infra: bool | None,
    keep_browser: bool = True,
    keep_shell_sessions: bool = False,
    teardown_extras: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
) -> _Effects:
    # meiva:note
    # Run a normal stop whose host effects are recorded instead of performed.
    dependencies(monkeypatch, machine=command_runtime.machine)
    command_runtime.machine.set(role=roles)
    effects = _Effects()
    effects.root_stops = record_root_stops(monkeypatch)

    def recording(name: str) -> Callable[..., list[str]]:
        def effect(*_args: object, **_kwargs: object) -> list[str]:
            effects.done.append(name)
            return []

        return effect

    for name in ("close_shell_sessions", "stop_browser", "stop_extras", "stop_data_plane"):
        monkeypatch.setattr(command, name, recording(name))
    rc = command.stop(
        require_confirmation=False,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        keep_shell_sessions=keep_shell_sessions,
        announce=False,
        teardown_extras=teardown_extras,
        timeout=5.0,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )
    assert rc == 0
    return effects


@pytest.mark.parametrize("keep_shell_sessions", [True, False])
def test_shell_sessions_are_closed_exactly_when_the_operator_does_not_keep_them(
    keep_shell_sessions: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    effects = _stop_effects(
        monkeypatch,
        command_runtime=command_runtime,
        operator_database=operator_database,
        operator_pipeline=operator_pipeline,
        roles="agent-runner",
        keep_infra=None,
        keep_shell_sessions=keep_shell_sessions,
    )
    assert ("close_shell_sessions" in effects.done) is not keep_shell_sessions
    assert effects.pty_sessions_stopped() is not keep_shell_sessions


@pytest.mark.parametrize("keep_browser", [True, False])
def test_the_browser_is_stopped_exactly_when_the_operator_does_not_keep_it(
    keep_browser: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    effects = _stop_effects(
        monkeypatch,
        command_runtime=command_runtime,
        operator_database=operator_database,
        operator_pipeline=operator_pipeline,
        roles="agent-runner",
        keep_infra=None,
        keep_browser=keep_browser,
    )
    assert ("stop_browser" in effects.done) is not keep_browser


@pytest.mark.parametrize("teardown_extras", [True, False])
def test_extras_are_torn_down_exactly_when_the_operator_asks(
    teardown_extras: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    effects = _stop_effects(
        monkeypatch,
        command_runtime=command_runtime,
        operator_database=operator_database,
        operator_pipeline=operator_pipeline,
        roles="agent-runner",
        keep_infra=None,
        teardown_extras=teardown_extras,
    )
    assert ("stop_extras" in effects.done) is teardown_extras


@pytest.mark.parametrize("keep_infra", [True, False])
def test_a_gateway_stops_its_data_plane_exactly_when_the_operator_does_not_keep_it(
    keep_infra: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    effects = _stop_effects(
        monkeypatch,
        command_runtime=command_runtime,
        operator_database=operator_database,
        operator_pipeline=operator_pipeline,
        roles="gateway",
        keep_infra=keep_infra,
    )
    assert ("stop_data_plane" in effects.done) is not keep_infra


def test_a_host_without_the_gateway_capability_has_no_data_plane_phase(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    effects = _stop_effects(
        monkeypatch,
        command_runtime=command_runtime,
        operator_database=operator_database,
        operator_pipeline=operator_pipeline,
        roles="agent-runner",
        keep_infra=None,
    )
    assert "stop_data_plane" not in effects.done


@pytest.mark.parametrize("keep_infra", [True, False])
def test_a_host_without_the_gateway_capability_refuses_a_data_plane_choice(
    keep_infra: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    with pytest.raises(ValueError, match="no local data plane"):
        _stop_effects(
            monkeypatch,
            command_runtime=command_runtime,
            operator_database=operator_database,
            operator_pipeline=operator_pipeline,
            roles="agent-runner",
            keep_infra=keep_infra,
        )


def test_a_gateway_requires_a_data_plane_choice(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    with pytest.raises(ValueError, match="choose --keep-infra or --stop-infra"):
        _stop_effects(
            monkeypatch,
            command_runtime=command_runtime,
            operator_database=operator_database,
            operator_pipeline=operator_pipeline,
            roles="gateway",
            keep_infra=None,
        )


@pytest.mark.parametrize("dedicated", [SERVICE_UNIT, "browser"])
def test_keep_service_cannot_name_a_service_with_its_own_choice(
    dedicated: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    with pytest.raises(ValueError, match="--keep-shell-sessions"):
        _stop_effects(
            monkeypatch,
            command_runtime=command_runtime,
            operator_database=operator_database,
            operator_pipeline=operator_pipeline,
            roles="agent-runner",
            keep_infra=None,
            preserve_sessions=frozenset({dedicated}),
        )


# meiva:note
# ── the force path honours the same choices ──────────────────────────────────


def _force_stop(
    *,
    command_runtime: CommandRuntime,
    operator_pipeline: Callable[[], EventPipeline],
    keep_infra: bool | None,
    preserve_sessions: frozenset[str] = frozenset(),
    teardown_extras: bool = False,
) -> int:
    return entry.force_stop(
        Path("/unused"),
        require_confirmation=False,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=True,
        keep_shell_sessions=True,
        announce=False,
        teardown_extras=teardown_extras,
        timeout=9.0,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )


@pytest.mark.parametrize("dedicated", [SERVICE_UNIT, "browser"])
def test_force_stop_refuses_keep_service_for_a_service_with_its_own_choice(
    dedicated: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(role="agent-runner")
    root_stops = record_root_stops(monkeypatch)
    with pytest.raises(ValueError, match="--keep-shell-sessions"):
        _force_stop(
            command_runtime=command_runtime,
            operator_pipeline=operator_pipeline,
            keep_infra=None,
            preserve_sessions=frozenset({dedicated}),
        )
    assert root_stops == []


@pytest.mark.parametrize(("roles", "keep_infra"), [("agent-runner", True), ("gateway", None)])
def test_force_stop_refuses_a_data_plane_choice_the_host_does_not_match(
    roles: str,
    keep_infra: bool | None,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(role=roles)
    root_stops = record_root_stops(monkeypatch)
    with pytest.raises(ValueError, match=r"data plane|--keep-infra"):
        _force_stop(
            command_runtime=command_runtime,
            operator_pipeline=operator_pipeline,
            keep_infra=keep_infra,
        )
    assert root_stops == []


@pytest.mark.parametrize("teardown_extras", [True, False])
def test_force_stop_tears_down_extras_exactly_when_asked_and_spends_the_budget(
    teardown_extras: bool,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    command_runtime.machine.set(role="gateway")
    extras: list[float] = []
    root_stops = record_root_stops(monkeypatch)
    monkeypatch.setattr(command, "stop_extras", extras.append)
    assert (
        _force_stop(
            command_runtime=command_runtime,
            operator_pipeline=operator_pipeline,
            keep_infra=True,
            teardown_extras=teardown_extras,
        )
        == 0
    )
    assert bool(extras) is teardown_extras
    assert root_stops
    assert all(0 < call["timeout_s"] <= 9.0 for call in root_stops)


# meiva:note
# ── restart --only agent-host ────────────────────────────────────────────────
# The restart forwarding of every other choice is covered in
# identity/test_commands_restart_stop.py.


class _FakeRoot:
    # meiva:note
    # The root control client: records the lifecycle verbs it is asked for.

    def __init__(self, events: list[str], *, live: bool, down_ok: bool) -> None:
        self._events = events
        self._live = live
        self._down_ok = down_ok

    def status(self) -> dict[str, object]:
        return {"ok": self._live, "result": {"units": [{"id": "agent-host"}]}}

    def _reply(self, ok: bool) -> dict[str, object]:
        return {"ok": ok} if ok else {"ok": False, "code": 7, "error": "refused"}

    def down(self, unit: str) -> dict[str, object]:
        self._events.append(f"down:{unit}")
        return self._reply(self._down_ok)

    def force_down(self, unit: str) -> dict[str, object]:
        self._events.append(f"force_down:{unit}")
        return self._reply(self._down_ok)

    def up(self, unit: str) -> dict[str, object]:
        self._events.append(f"up:{unit}")
        return self._reply(True)


def _restart(
    events: list[str],
    *,
    force: bool = False,
    live: bool = True,
    down_ok: bool = True,
    ready: bool = True,
) -> int:
    client = _FakeRoot(events, live=live, down_ok=down_ok)
    timeouts: list[float] = []

    def client_for(seconds: float) -> _FakeRoot:
        timeouts.append(seconds)
        return client

    def wait(_spec: object) -> bool:
        events.append("wait-ready")
        return ready

    def audit(verb: str, units: Sequence[str]) -> None:
        events.append(f"audit:{verb}:{','.join(units)}")

    with lifecycle_audit(audit):
        rc = entry.restart_agent_host(
            force=force,
            timeout=12.0,
            client_for=client_for,
            require_owner=lambda _status: None,
            ready=wait,
        )
    assert set(timeouts) == {12.0}
    return rc


@pytest.mark.parametrize(("force", "verb"), [(False, "down"), (True, "force_down")])
def test_restart_agent_host_downs_then_ups_it_with_the_chosen_interruption(
    force: bool, verb: str
) -> None:
    events: list[str] = []
    assert _restart(events, force=force) == 0
    audited = "force-down" if force else "down"
    assert events == [
        f"audit:{audited}:agent-host",
        f"{verb}:agent-host",
        "audit:up:agent-host",
        "up:agent-host",
        "wait-ready",
    ]


def test_restart_agent_host_without_a_live_root_changes_nothing() -> None:
    events: list[str] = []
    assert _restart(events, live=False) == 1
    assert events == []


def test_restart_agent_host_fails_when_the_root_refuses_the_down() -> None:
    events: list[str] = []
    with pytest.raises(RootDriverError, match="down agent-host failed"):
        _restart(events, down_ok=False)
    assert events == ["audit:down:agent-host", "down:agent-host"], (
        "a refused down is not followed by up"
    )


def test_restart_agent_host_fails_when_it_does_not_become_ready() -> None:
    events: list[str] = []
    assert _restart(events, ready=False) == 1
    assert events[-3:] == ["audit:up:agent-host", "up:agent-host", "wait-ready"]
