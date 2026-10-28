from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from base.host.permissions_helper.client import (
    PermissionsHelperError,
    RequestGrantsResult,
    SpawnResult,
)
from base.host.permissions_helper.grants import probe
from base.host.permissions_helper.grants.catalog import (
    ACCESSIBILITY_ITEM,
    AUTOMATION_ITEMS,
    SCREEN_ITEM,
    Grant,
)


# meiva:note
# The waits a probe spends, recorded instead of slept: `sleep` moves the monotonic reading, so a
# poll loop reaches its deadline, and the test can read what it slept on. `base.host` may not
# import `base.clock` (that edge would close the cycle `clock -> config -> ... -> host`), so this
# test keeps its own double of the two verbs.
class RecordedWaits:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds


class FakeHelper:
    def __init__(
        self,
        *,
        codes: dict[str, int],
        screen: bool = True,
        ax: bool = True,
        request_capable: bool = True,
    ) -> None:
        self._codes = codes
        self._screen = screen
        self._ax = ax
        self._request_capable = request_capable
        self.spawned: list[list[str]] = []

    def ping(self) -> dict[str, object]:
        return {
            "preflight_screen": self._screen,
            "ax_trusted": self._ax,
            "request_grants_v1": self._request_capable,
        }

    def request_grants(self) -> RequestGrantsResult:
        return {"screen_recording": self._screen, "accessibility": self._ax}

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> SpawnResult:
        self.spawned.append(list(argv))
        services = json.loads(argv[2])
        Path(argv[3]).write_text(
            json.dumps({service: {"code": self._codes[service]} for service in services})
        )
        return {"pid": 4242, "reused": False}


class DeadHelper:
    def ping(self) -> dict[str, object]:
        raise PermissionsHelperError("socket refused the connection")

    def request_grants(self) -> RequestGrantsResult:
        raise PermissionsHelperError("socket refused the connection")

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> SpawnResult:
        raise PermissionsHelperError("socket refused the connection")


def _codes(value: int) -> dict[str, int]:
    return dict.fromkeys(probe.PREFLIGHT_SERVICES, value)


def test_the_probe_reads_every_catalog_service_through_one_child(tmp_path: Path) -> None:
    helper = FakeHelper(codes=_codes(0))
    result = probe.probe(helper, workdir=tmp_path, run_id="r1", env={}, timers=RecordedWaits())
    assert len(helper.spawned) == 1
    assert result.helper_reachable
    assert all(grant is Grant.GRANTED for grant in result.grants.values())
    assert set(result.grants) == set(probe.PREFLIGHT_SERVICES)


def test_preflight_words_map_onto_the_grant_vocabulary(tmp_path: Path) -> None:
    helper = FakeHelper(codes=_codes(2))
    result = probe.probe(helper, workdir=tmp_path, run_id="r2", env={}, timers=RecordedWaits())
    assert all(grant is Grant.NOT_DETERMINED for grant in result.grants.values())


def test_a_grant_the_record_holds_and_the_helper_has_not_read_is_stale(tmp_path: Path) -> None:
    helper = FakeHelper(codes=_codes(0), screen=False, ax=True)
    result = probe.probe(helper, workdir=tmp_path, run_id="r3", env={}, timers=RecordedWaits())
    assert result.grant_for(SCREEN_ITEM) is Grant.STALE
    assert result.grant_for(ACCESSIBILITY_ITEM) is Grant.GRANTED
    assert result.grant_for(AUTOMATION_ITEMS[0]) is Grant.UNKNOWN


def test_a_record_without_a_decision_stays_not_determined(tmp_path: Path) -> None:
    helper = FakeHelper(codes=_codes(2), screen=False, ax=False)
    result = probe.probe(helper, workdir=tmp_path, run_id="r8", env={}, timers=RecordedWaits())
    assert result.grant_for(SCREEN_ITEM) is Grant.NOT_DETERMINED
    assert result.grant_for(ACCESSIBILITY_ITEM) is Grant.NOT_DETERMINED


def test_a_helper_without_the_request_method_says_so(tmp_path: Path) -> None:
    helper = FakeHelper(codes=_codes(0), request_capable=False)
    result = probe.probe(helper, workdir=tmp_path, run_id="r7", env={}, timers=RecordedWaits())
    assert not result.supports_request


def test_the_request_reports_what_the_helper_holds_afterwards() -> None:
    helper = FakeHelper(codes=_codes(0), screen=False, ax=False)
    assert probe.request_process_grants(helper) == {
        "screen_recording": False,
        "accessibility": False,
    }


def test_a_helper_that_does_not_answer_is_unavailable_not_denied(tmp_path: Path) -> None:
    result = probe.probe(
        DeadHelper(), workdir=tmp_path, run_id="r4", env={}, timers=RecordedWaits()
    )
    assert not result.helper_reachable
    assert result.grants == {}
    assert "launchctl" in result.diagnostic


def test_an_old_helper_build_is_a_setup_error_the_caller_must_fix(tmp_path: Path) -> None:
    class OldHelper(FakeHelper):
        def spawn_process(self, *args: object, **kwargs: object) -> Any:
            raise PermissionsHelperError("unknown method: spawn")

    with pytest.raises(probe.PermissionsProbeError) as caught:
        probe.probe(
            OldHelper(codes=_codes(0)),
            workdir=tmp_path,
            run_id="r5",
            env={},
            timers=RecordedWaits(),
        )
    assert "rebuild" in str(caught.value)


# meiva:note
# The waits of one preflight, answered from a script: `monotonic` walks the readings and
# `sleep` records the interval. A poll that slept on the process clock instead leaves `sleeps`
# empty, and the scripted reading past the deadline still ends the loop, so the test reports
# instead of hanging.
class ScriptedWaits:
    def __init__(self, readings: list[float]) -> None:
        self._readings = readings
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._readings.pop(0) if self._readings else 1_000.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


# meiva:note
# A preflight that finds no result spends its poll on the timers it was given, and the deadline it
# compares against is the same value's.
def test_a_preflight_that_finds_no_result_spends_its_poll_on_the_given_timers(
    tmp_path: Path,
) -> None:
    class Silent(FakeHelper):
        def spawn_process(self, *args: object, **kwargs: object) -> Any:
            return {"pid": 1, "reused": False}

    waits = ScriptedWaits([0.0, 0.0, 5.0])
    with pytest.raises(probe.PermissionsProbeError):
        probe.run_preflight(
            Silent(codes=_codes(0)),
            workdir=tmp_path,
            run_id="r9",
            env={},
            services=("kTCCServiceScreenCapture",),
            timers=waits,
            wait_s=1.0,
        )

    assert waits.sleeps == [probe.POLL_S]


def test_a_probe_child_that_never_writes_times_out(tmp_path: Path) -> None:
    class Silent(FakeHelper):
        def spawn_process(self, *args: object, **kwargs: object) -> Any:
            return {"pid": 1, "reused": False}

    with pytest.raises(probe.PermissionsProbeError):
        probe.run_preflight(
            Silent(codes=_codes(0)),
            workdir=tmp_path,
            run_id="r6",
            env={},
            services=("kTCCServiceScreenCapture",),
            wait_s=0.0,
            timers=RecordedWaits(),
        )
