from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from base.host.permissions_helper.client import (
    PermissionsHelperClient,
    PermissionsHelperError,
    SpawnResult,
)
from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.triggers import FOLDER_PATHS
from services.desktop.permissions_helper import onboarding

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)


class FakeHelper(PermissionsHelperClient):
    def __init__(
        self,
        codes: dict[str, int],
        *,
        screen: bool = True,
        ax: bool = True,
        request_capable: bool = True,
        request_fails: bool = False,
    ) -> None:
        super().__init__("/unused.sock")
        self.codes = codes
        self.screen = screen
        self.ax = ax
        self.request_capable = request_capable
        self.request_fails = request_fails
        self.spawns: list[list[str]] = []
        self.children: list[list[str]] = []
        self.requests = 0

    def ping(self) -> Any:
        return {
            "pong": True,
            "preflight_screen": self.screen,
            "ax_trusted": self.ax,
            "request_grants_v1": self.request_capable,
        }

    def request_grants(self) -> Any:
        self.requests += 1
        if self.request_fails:
            raise PermissionsHelperError("socket went away mid-call")
        return {"screen_recording": self.screen, "accessibility": self.ax}

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> SpawnResult:
        child = Path(argv[1]).name
        if child == "folder_touch.py":
            # meiva:note
            # The trigger child of a folder item runs for real against the test home: it lists
            # the folder and writes what happened where the gate reads it.
            self.children.append(list(argv))
            subprocess.run(argv, check=True)  # noqa: S603
            return {"pid": 1, "reused": False}
        if child == "apple_event.py":
            # meiva:note
            # An Automation child needs osascript and a person at a macOS dialog, which a test
            # host has neither of: the helper reports the dialog as accepted.
            self.children.append(list(argv))
            Path(argv[3]).write_text("granted")
            return {"pid": 1, "reused": False}
        self.spawns.append(list(argv))
        services = json.loads(argv[2])
        Path(argv[3]).write_text(
            json.dumps({service: {"code": self.codes[service]} for service in services})
        )
        return {"pid": 1, "reused": False}


class DeadHelper(PermissionsHelperClient):
    def __init__(self) -> None:
        super().__init__("/unused.sock")

    def ping(self) -> Any:
        raise PermissionsHelperError("no socket")


def _codes(value: int) -> dict[str, int]:
    return dict.fromkeys(PREFLIGHT_SERVICES, value)


def host(
    tmp_path: Path, *, client: FakeHelper | DeadHelper | None = None, platform: str = "darwin"
) -> onboarding.GateHost:
    for subpath in FOLDER_PATHS.values():
        (tmp_path / subpath).mkdir(parents=True, exist_ok=True)
    return onboarding.GateHost(
        client=client if client is not None else FakeHelper(_codes(0)),
        env={},
        workdir=tmp_path / "scratch",
        home=tmp_path,
        record_path=tmp_path / gate.RECORD_NAME,
        platform=platform,
    )


def test_check_probes_and_remembers_what_it_saw(tmp_path: Path) -> None:
    seat = host(tmp_path)
    report = onboarding.check(host=seat, now=NOW)
    assert report.state is gate.GateState.COMPLETE
    stored = gate.read_record(seat.record_path)
    assert stored is not None
    assert stored.state is gate.GateState.COMPLETE
    assert stored.grants["screen-recording"] is Grant.GRANTED


def test_a_deferred_host_stays_deferred_across_checks(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(2)))
    onboarding.defer(now=NOW, host=seat)
    report = onboarding.check(host=seat, now=NOW)
    assert report.state is gate.GateState.DEFERRED
    assert report.restricted


def test_an_unreachable_helper_is_reported_as_unavailable(tmp_path: Path) -> None:
    report = onboarding.check(host=host(tmp_path, client=DeadHelper()), now=NOW)
    assert report.state is gate.GateState.UNAVAILABLE


def test_a_non_darwin_host_has_nothing_to_check(tmp_path: Path) -> None:
    report = onboarding.check(host=host(tmp_path, client=DeadHelper(), platform="linux"), now=NOW)
    assert report.state is gate.GateState.NOT_APPLICABLE
    assert not report.restricted


def test_the_first_pass_sees_both_desktop_grants_and_each_folder(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(2), screen=False, ax=False))
    report = onboarding.check(host=seat, now=NOW)
    assert [item.id for item in report.prompt_now()] == [
        "screen-recording",
        "accessibility",
        "desktop-folder",
        "documents-folder",
        "downloads-folder",
    ]


def test_the_first_pass_asks_both_desktop_grants_and_each_folder(tmp_path: Path) -> None:
    helper = FakeHelper(_codes(2), screen=False, ax=False)
    seat = host(tmp_path, client=helper)

    onboarding.onboard(host=seat, now=NOW)

    asked = {argv[2] for argv in helper.children}
    assert helper.requests == 1
    assert {str(tmp_path / FOLDER_PATHS[item]) for item in FOLDER_PATHS} == asked
    assert len(helper.spawns) == 2


def test_each_request_is_narrated_on_the_stream_it_is_given(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(2), screen=False, ax=False))
    progress = io.StringIO()

    onboarding.onboard(host=seat, now=NOW, progress=progress)

    lines = progress.getvalue().splitlines()
    assert lines[0] == "Asking the system for: screen-recording, accessibility"
    assert [line for line in lines if line.startswith("Asking for: ")]
    assert lines.count("  -> granted") == len(
        [line for line in lines if line.startswith("Asking for")]
    )


def test_the_request_outcome_is_not_its_own_evidence(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(2), screen=False, ax=False))
    report = onboarding.onboard(
        host=seat,
        now=NOW,
    )
    assert report.state is gate.GateState.ACTION_REQUIRED
    assert report.grant_for("desktop-folder") is Grant.NOT_DETERMINED
    assert report.grant_for("screen-recording") is Grant.NOT_DETERMINED


def test_a_denied_grant_is_not_asked_again_until_a_person_asks(tmp_path: Path) -> None:
    codes = _codes(2)
    helper = FakeHelper(codes, screen=False, ax=False)
    seat = host(tmp_path, client=helper)
    first = onboarding.onboard(host=seat, now=NOW)
    assert helper.requests == 1
    assert first.grant_for("screen-recording") is Grant.NOT_DETERMINED

    codes.update(_codes(1))
    second = onboarding.onboard(host=seat, now=NOW)
    assert helper.requests == 1
    assert second.grant_for("screen-recording") is Grant.DENIED
    assert [item.id for item in second.manual()] == [
        "screen-recording",
        "accessibility",
        "app-management",
        "desktop-folder",
        "documents-folder",
        "downloads-folder",
    ]

    onboarding.onboard(host=seat, now=NOW, automatic=False)
    assert helper.requests == 2


def test_a_grant_the_record_holds_and_the_helper_has_not_read_is_stale(tmp_path: Path) -> None:
    seat = host(tmp_path, client=FakeHelper(_codes(0), screen=False, ax=False))
    report = onboarding.check(host=seat, now=NOW)
    assert report.state is gate.GateState.ACTION_REQUIRED
    assert report.grant_for("screen-recording") is Grant.STALE
    assert report.prompt_now() == ()


def test_a_helper_without_the_request_method_keeps_the_manual_path(tmp_path: Path) -> None:
    helper = FakeHelper(_codes(2), screen=False, ax=False, request_capable=False)
    seat = host(tmp_path, client=helper)
    report = onboarding.onboard(host=seat, now=NOW)
    assert helper.requests == 0
    assert "request_grants_v1" in report.diagnostic
    assert report.grant_for("screen-recording") is Grant.NOT_DETERMINED


# meiva:note
# App Management is never asked, and no build may start asking: tccd answers a request for
# it with "Service kTCCServiceSystemPolicyAppBundles does not allow prompting for
# unentitled binaries; returning denied" and records that policy denial against the helper,
# so asking would raise no dialog and stamp a fresh denial on every rebuilt helper
# (measured 2026-10-21). This test is the lock.
def test_app_management_is_never_asked_even_as_the_only_missing_desktop_row(
    tmp_path: Path,
) -> None:
    codes = _codes(0)
    codes["kTCCServiceSystemPolicyAppBundles"] = 2
    helper = FakeHelper(codes)
    report = onboarding.onboard(
        host=host(tmp_path, client=helper),
        now=NOW,
        automatic=False,
    )
    assert helper.requests == 0
    assert "app-management" not in {item.id for item in report.requestable()}
    assert "app-management" not in {item.id for item in report.prompt_now()}
    assert "app-management" in {item.id for item in report.manual()}
    assert report.grant_for("app-management") is Grant.NOT_DETERMINED


def test_the_summary_names_what_is_missing(tmp_path: Path) -> None:
    report = onboarding.check(host=host(tmp_path, client=FakeHelper(_codes(1))), now=NOW)
    summary = onboarding.missing_summary(report)
    assert summary.startswith("4 permission(s) still missing")
    assert "Screen Recording" not in summary
    assert onboarding.observations(report)["desktop-folder"] == "denied"


def test_a_stale_helper_is_never_asked_even_by_the_manual_pass(tmp_path: Path) -> None:
    helper = FakeHelper(_codes(0), screen=False, ax=False)
    seat = host(tmp_path, client=helper)
    report = onboarding.onboard(host=seat, now=NOW, automatic=False)
    assert helper.requests == 0
    assert report.grant_for("screen-recording") is Grant.STALE


def test_a_failed_request_is_reported_rather_than_crashing_the_start(tmp_path: Path) -> None:
    helper = FakeHelper(_codes(2), screen=False, ax=False, request_fails=True)
    seat = host(tmp_path, client=helper)
    report = onboarding.onboard(host=seat, now=NOW)
    assert helper.requests == 1
    assert "socket went away mid-call" in report.diagnostic
    assert report.state is gate.GateState.ACTION_REQUIRED


def test_the_restart_verb_prints_the_custody_path() -> None:
    sentence = onboarding.restart_helper()
    assert "meiva stop" in sentence and "meiva start" in sentence
    assert "kickstart" in sentence
