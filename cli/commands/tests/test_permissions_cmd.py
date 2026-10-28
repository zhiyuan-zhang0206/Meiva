from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from base.clock.ids import new_uuid
from base.clock.tests.fakes import clock_at
from base.clock.tests.manual_timers import ManualTimers
from base.clock.tests.seeded_rng import seeded_rng
from base.host.permissions_helper.client import PermissionsHelperClient
from base.host.permissions_helper.grants import checklist, gate
from base.host.permissions_helper.grants.catalog import PING_KEYS, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult, RequestGrantsResult
from base.host.permissions_helper.grants.triggers import FOLDER_PATHS
from base.sessions.tests.scripted_helper import serve
from cli.commands import permissions as cmd
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.desktop.permissions_helper import onboarding
from services.desktop.permissions_helper.onboarding import GateHost

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
RUN = "run-7731"


def _report(**overrides: Grant) -> gate.GateReport:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    booleans = dict.fromkeys(PING_KEYS, True)
    for item_id, grant in overrides.items():
        item = gate.item_for(item_id)
        if item.ping_key:
            booleans[item.ping_key] = grant is Grant.GRANTED
        for service in item.services:
            grants[service] = grant
    return gate.evaluate(
        ProbeResult(grants=grants, booleans=booleans, supports_request=True),
        now=NOW,
    )


def _args(**kwargs: Any) -> argparse.Namespace:
    base = {"permissions_cmd": "status", "json": False, "timeout": 90.0}
    base.update(kwargs)
    return argparse.Namespace(**base)


class FakeHelper:
    def __init__(self, **overrides: Grant) -> None:
        self._codes = dict.fromkeys(PREFLIGHT_SERVICES, 0)
        self._booleans = dict.fromkeys(PING_KEYS, True)
        for item_id, grant in overrides.items():
            item = gate.item_for(item_id)
            if item.ping_key:
                self._booleans[item.ping_key] = grant is Grant.GRANTED
            code = {Grant.GRANTED: 0, Grant.DENIED: 1, Grant.NOT_DETERMINED: 2}[grant]
            for service in item.services:
                self._codes[service] = code
        self.requests = 0
        self.children: list[tuple[dict[str, str], str]] = []
        self.folders: list[str] = []

    def ping(self) -> Mapping[str, object]:
        return {
            "pong": True,
            "request_grants_v1": True,
            **self._booleans,
        }

    def request_grants(self) -> RequestGrantsResult:
        self.requests += 1
        self._codes = dict.fromkeys(PREFLIGHT_SERVICES, 0)
        self._booleans = dict.fromkeys(PING_KEYS, True)
        return {
            "screen_recording": True,
            "accessibility": True,
        }

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> Mapping[str, object]:
        self.children.append((env, cwd))
        if Path(argv[1]).name == "folder_touch.py":
            self.folders.append(argv[2])
            Path(argv[3]).write_text("granted")
            return {"pid": 1, "reused": False}
        services = json.loads(argv[2])
        Path(argv[3]).write_text(
            json.dumps({service: {"code": self._codes[service]} for service in services})
        )
        return {"pid": 1, "reused": False}


def _host(tmp_path: Path, **overrides: Grant) -> tuple[GateHost, FakeHelper]:
    helper = FakeHelper(**overrides)
    seat = GateHost(
        client=helper,
        env={},
        workdir=tmp_path / "scratch",
        home=tmp_path,
        record_path=tmp_path / gate.RECORD_NAME,
        timers=ManualTimers(),
    )
    return seat, helper


def test_status_exits_nonzero_while_something_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path, **{"screen-recording": Grant.DENIED})
    assert cmd.dispatch(_args(), host=seat, clock=clock_at(NOW), run_id=RUN) == 1
    assert "Screen Recording" in capsys.readouterr().out


def test_status_exits_zero_on_a_complete_host(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path)
    assert cmd.dispatch(_args(), host=seat, clock=clock_at(NOW), run_id=RUN) == 0
    assert "Every required authorization is granted." in capsys.readouterr().out


def test_json_output_is_the_checklist_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(**{"desktop-folder": Grant.NOT_DETERMINED})
    seat, _ = _host(tmp_path, **{"desktop-folder": Grant.NOT_DETERMINED})
    cmd.dispatch(_args(json=True), host=seat, clock=clock_at(NOW), run_id=RUN)
    payload = json.loads(capsys.readouterr().out)
    assert payload == checklist.payload(report)
    assert payload["required_missing"] == 1


def test_the_cli_onboard_is_the_manual_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, helper = _host(tmp_path, **{"screen-recording": Grant.DENIED})
    assert (
        cmd.dispatch(_args(permissions_cmd="onboard"), host=seat, clock=clock_at(NOW), run_id=RUN)
        == 0
    )
    assert helper.requests == 1
    assert "Every required authorization is granted." in capsys.readouterr().out


def test_defer_says_plainly_that_nothing_was_granted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path)
    assert (
        cmd.dispatch(_args(permissions_cmd="defer"), host=seat, clock=clock_at(NOW), run_id=RUN)
        == 0
    )
    assert "Nothing is granted by this" in capsys.readouterr().out


def test_restart_helper_prints_the_operator_sentence(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run(_args(permissions_cmd="restart-helper")) == 0
    assert "meiva stop" in capsys.readouterr().out


# meiva:note
# The verb describes its host from the invocation's own runtime: the home it operates (the record and
# the scratch directory live there), the helper its platform decided, the environment the
# configuration reads now (not the one the command host started with), its clock and the id it draws
# from its generator.
def test_the_verb_describes_the_host_from_the_invocation_runtime(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    helper = FakeHelper()
    home = tmp_path / "meiva-home"
    runtime = replace(
        command_runtime,
        home=home,
        helper=serve(cast("PermissionsHelperClient", helper)),
        host=replace(command_runtime.host, environ={"PATH": "/the/hosts/launch/path"}),
    )
    args = _args()
    args.runtime = runtime

    assert cmd.run(args) == 0

    stored = gate.read_record(gate.record_path(home))
    assert stored is not None
    assert stored.checked_at == runtime.clock.now()
    run_id = new_uuid(seeded_rng()).hex
    assert (home / "permissions-onboarding" / f"preflight-{run_id}.results.json").exists()
    [(env, cwd)] = helper.children
    assert cwd == str(home / "permissions-onboarding")
    forwarded = onboarding.gate_host(
        home=home,
        user_home=runtime.session.user_home,
        environ=runtime.host_after_config().environ,
        helper=None,
        timers=runtime.timers,
    ).env
    from_the_hosts_launch = onboarding.gate_host(
        home=home,
        user_home=runtime.session.user_home,
        environ=runtime.host.environ,
        helper=None,
        timers=runtime.timers,
    ).env
    assert env == forwarded
    assert env != from_the_hosts_launch


# meiva:note
# The folders a manual pass asks are the account's, taken from the session of the invocation: the
# session's home is a directory the Meiva home is not inside.
def test_the_manual_pass_asks_the_folders_of_the_session_account(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    helper = FakeHelper(**{"desktop-folder": Grant.NOT_DETERMINED})
    runtime = replace(
        command_runtime,
        home=tmp_path / "meiva-home",
        helper=serve(cast("PermissionsHelperClient", helper)),
    )
    args = _args(permissions_cmd="onboard")
    args.runtime = runtime

    cmd.run(args)

    assert helper.folders == [str(runtime.session.user_home / FOLDER_PATHS["desktop-folder"])]
    run_id = new_uuid(seeded_rng()).hex
    workdir = runtime.home / "permissions-onboarding"
    assert (workdir / f"desktop-folder-{run_id}-ask.result").exists()
    stored = gate.read_record(gate.record_path(runtime.home))
    assert stored is not None
    assert stored.checked_at == runtime.clock.now()


# meiva:note
# `meiva permissions defer` records the instant of the invocation's own clock.
def test_defer_records_the_instant_of_the_invocation_clock(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    runtime = replace(
        command_runtime,
        home=tmp_path / "meiva-home",
        helper=serve(cast("PermissionsHelperClient", FakeHelper())),
    )
    args = _args(permissions_cmd="defer")
    args.runtime = runtime

    assert cmd.run(args) == 0

    stored = gate.read_record(gate.record_path(runtime.home))
    assert stored is not None
    assert stored.state is gate.GateState.DEFERRED
    assert stored.checked_at == runtime.clock.now()
