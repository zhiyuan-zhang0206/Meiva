# meiva:note
# The permissions-helper converge steps: bring-up, screen capture, Accessibility, onboarding.
#
# Whether a helper can exist on a host is decided from the platform, the setting and the
# programs the host finds, so these tests hand in the `Host` they want: `capable_mac()` has the
# Xcode tools, the default Linux host does not.

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from base.clock.tests.fakes import clock_at
from base.host.converge.accessibility import (
    AccessibilityState,
    AccessibilityStatus,
)
from base.host.converge.accessibility import read_status as read_accessibility_status
from base.host.converge.accessibility import write_status as write_accessibility_status
from base.host.converge.screen_capture import (
    ScreenCaptureState,
    ScreenCaptureStatus,
    read_status,
    write_status,
)
from base.host.permissions_helper.client import HelperFactory, PermissionsHelperClient
from base.host.permissions_helper.grants import gate
from base.host.permissions_helper.grants.catalog import PING_KEYS
from base.host.permissions_helper.grants.triggers import FOLDER_PATHS
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from cli.commands.converge.plans.helper_plan import (
    BringUpHelper,
    NoHelperPlatform,
    RefuseBringUp,
    helper_bring_up_plan,
    helper_present,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host
from cli.commands.lifecycle.tests.converge_host.context_inputs import capable_mac
from services.desktop.permissions_helper import onboarding
from services.desktop.permissions_helper.launchd_job import LaunchdFacts

_BRING_UP = "permissions helper build + sign + load"
_NO_HELPER_HERE = "macOS only (permissions helper drives the desktop)"


def _bring_up(ctx: ConvergeCtx) -> None:
    (step,) = (step for step in converge_host.CONVERGE_STEPS if step.name == _BRING_UP)
    step.apply(ctx)


# meiva:note
# --- the decisions (pure) -----------------------------------------------------------


# meiva:note
# The bring-up's inputs come from the context and the configured values, each one distinct from the
# environment this test runs in: the home, the account's home, the port and the artifact directory.
def test_the_helper_bring_up_runs_in_what_the_context_and_the_config_name(
    ctx: ConvergeCtx, tmp_path: Path
) -> None:
    mac = replace(ctx, host=capable_mac(), meiva_home=tmp_path / "unit-home")
    artifact = tmp_path / "artifact"

    env = converge_host.helper_install_env(mac, helper_port=9301, artifact_dir=artifact)

    assert (env.helper_port, env.artifact_dir) == (9301, artifact)
    assert env.build.home == tmp_path / "unit-home"
    assert env.build.facts == LaunchdFacts(
        uid=mac.host.uid, platform="darwin", user_home=mac.session.user_home
    )
    assert env.build.run is mac.host.run
    assert env.build.timers is mac.timers and env.build.clock is mac.clock
    assert env.build.rng is mac.secure_rng


def test_only_a_mac_gets_a_helper_bring_up() -> None:
    assert helper_bring_up_plan(system="linux", enabled=True, incapability=None) == (
        NoHelperPlatform()
    )
    assert helper_bring_up_plan(system="darwin", enabled=True, incapability=None) == (
        BringUpHelper()
    )


def test_a_mac_that_switched_the_helper_off_cannot_start_a_root() -> None:
    assert helper_bring_up_plan(system="darwin", enabled=False, incapability=None) == RefuseBringUp(
        "macOS root supervision requires the permissions helper"
    )


def test_a_mac_missing_the_xcode_tools_cannot_start_a_root() -> None:
    plan = helper_bring_up_plan(
        system="darwin", enabled=True, incapability="no swiftc (install the Xcode tools)"
    )

    assert plan == RefuseBringUp(
        "macOS root supervision cannot start: no swiftc (install the Xcode tools)"
    )


@pytest.mark.parametrize(
    ("enabled", "incapability", "present"),
    [(True, None, True), (False, None, False), (True, _NO_HELPER_HERE, False)],
)
def test_a_helper_question_is_asked_only_where_a_helper_can_exist(
    enabled: bool, incapability: str | None, present: bool
) -> None:
    assert helper_present(enabled=enabled, incapability=incapability) is present


# meiva:note
# --- bring-up ------------------------------------------------------------------------


def test_bring_up_is_a_no_op_off_macos(ctx: ConvergeCtx) -> None:
    # meiva:note
    # a Linux host: nothing to build, no helper to ask for
    _bring_up(ctx)


def test_a_mac_with_the_helper_switched_off_fails_converge(ctx: ConvergeCtx) -> None:
    mac = replace(ctx, host=capable_mac())
    mac.config.set_field("permissions_helper_enabled", False)

    with pytest.raises(RuntimeError, match="requires the permissions helper"):
        _bring_up(mac)


def test_a_mac_without_swiftc_fails_converge_naming_the_missing_tool(ctx: ConvergeCtx) -> None:
    no_swiftc = fake_host(platform=DARWIN_ARM64, programs={"codesign": "/usr/bin/codesign"})
    mac = replace(ctx, host=no_swiftc)
    mac.config.set_field("permissions_helper_enabled", True)

    with pytest.raises(RuntimeError, match="cannot start: no swiftc"):
        _bring_up(mac)


# meiva:note
# --- a host that can run the helper never runs without one ---------------------------


@pytest.mark.parametrize(
    "step",
    [
        pytest.param(_bring_up, id="bring-up"),
        pytest.param(converge_host.ensure_screen_capture, id="screen-capture"),
        pytest.param(converge_host.ensure_accessibility, id="accessibility"),
    ],
)
def test_a_host_that_can_run_the_helper_never_runs_without_one(
    step: Callable[[ConvergeCtx], None], ctx: ConvergeCtx, unit_home: Path
) -> None:
    mac = replace(ctx, host=capable_mac(), helper=None, meiva_home=unit_home)
    mac.config.set_field("permissions_helper_enabled", True)

    with pytest.raises(RuntimeError, match="platform has none"):
        step(mac)


# meiva:note
# --- screen capture ------------------------------------------------------------------


def _mac(ctx: ConvergeCtx, home: Path, helper: HelperFactory) -> ConvergeCtx:
    mac = replace(ctx, host=capable_mac(), meiva_home=home, helper=helper)
    mac.config.set_field("permissions_helper_enabled", True)
    return mac


def test_screen_capture_step_records_the_helpers_answer(
    ctx: ConvergeCtx, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = ScreenCaptureStatus(
        state=ScreenCaptureState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    mac = _mac(ctx, unit_home, serve(ScriptedHelper(check_screen_capture=status)))

    converge_host.ensure_screen_capture(mac)

    written = read_status(unit_home)
    assert written is not None
    assert written.state is ScreenCaptureState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err


def test_screen_capture_step_clears_a_stale_file_when_the_grant_is_back(
    ctx: ConvergeCtx, unit_home: Path
) -> None:
    available = ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE)
    mac = _mac(ctx, unit_home, serve(ScriptedHelper(check_screen_capture=available)))
    write_status(
        unit_home, ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale")
    )

    converge_host.ensure_screen_capture(mac)

    assert read_status(unit_home) is None


# meiva:note
# Nothing to ask when no helper can exist here -- and the helper step has
# already said so, making a second derived complaint noise rather than news.
@pytest.mark.parametrize("why", ["disabled", "incapable_host"])
def test_screen_capture_step_skips_hosts_with_no_helper(
    why: str, ctx: ConvergeCtx, unit_home: Path
) -> None:
    # meiva:note
    # a Linux host: no helper can run
    host_ctx = replace(ctx, meiva_home=unit_home)
    host_ctx.config.set_field("permissions_helper_enabled", why != "disabled")
    if why == "disabled":
        host_ctx = replace(host_ctx, host=capable_mac())
    write_status(
        unit_home, ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale")
    )

    # meiva:note
    # the refusing helper is never asked
    converge_host.ensure_screen_capture(host_ctx)

    assert read_status(unit_home) is None


# meiva:note
# --- Accessibility -------------------------------------------------------------------


def test_accessibility_step_records_the_helpers_answer(
    ctx: ConvergeCtx, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = AccessibilityStatus(
        state=AccessibilityState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    mac = _mac(ctx, unit_home, serve(ScriptedHelper(check_accessibility=status)))

    converge_host.ensure_accessibility(mac)

    written = read_accessibility_status(unit_home)
    assert written is not None
    assert written.state is AccessibilityState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err


def test_accessibility_step_clears_a_stale_file_when_the_grant_is_back(
    ctx: ConvergeCtx, unit_home: Path
) -> None:
    granted = AccessibilityStatus(state=AccessibilityState.GRANTED)
    mac = _mac(ctx, unit_home, serve(ScriptedHelper(check_accessibility=granted)))
    write_accessibility_status(
        unit_home, AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale")
    )

    converge_host.ensure_accessibility(mac)

    assert read_accessibility_status(unit_home) is None


@pytest.mark.parametrize("why", ["disabled", "incapable_host"])
def test_accessibility_step_skips_hosts_with_no_helper(
    why: str, ctx: ConvergeCtx, unit_home: Path
) -> None:
    host_ctx = replace(ctx, meiva_home=unit_home)
    host_ctx.config.set_field("permissions_helper_enabled", why != "disabled")
    if why == "disabled":
        host_ctx = replace(host_ctx, host=capable_mac())
    write_accessibility_status(
        unit_home, AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale")
    )

    converge_host.ensure_accessibility(host_ctx)

    assert read_accessibility_status(unit_home) is None


def test_accessibility_step_follows_the_screen_capture_step() -> None:
    steps = list(converge_host.CONVERGE_STEPS)
    screen_index = next(
        i for i, step in enumerate(steps) if step.name == "screen capture availability"
    )
    screen_step = steps[screen_index]
    accessibility_step = steps[screen_index + 1]

    assert accessibility_step.name == "accessibility availability"
    assert accessibility_step.apply is converge_host.ensure_accessibility
    assert accessibility_step.roles == screen_step.roles
    assert accessibility_step.requires_unit_config == screen_step.requires_unit_config


# meiva:note
# A helper that answers every probe with a grant and records where its children were asked to run.
class _PreflightHelper:
    def __init__(self) -> None:
        self.cwds: list[str] = []
        self.envs: list[dict[str, str]] = []
        self.folders: list[str] = []

    def ping(self) -> Mapping[str, object]:
        return {"pong": True, "request_grants_v1": True, **dict.fromkeys(PING_KEYS, True)}

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> Mapping[str, object]:
        del name, stdout, stderr
        self.cwds.append(cwd)
        self.envs.append(env)
        if Path(argv[1]).name == "folder_touch.py":
            self.folders.append(argv[2])
            Path(argv[3]).write_text("granted")
            return {"pid": 1, "reused": False}
        undetermined = set(gate.item_for("desktop-folder").services)
        Path(argv[3]).write_text(
            json.dumps(
                {
                    service: {"code": 2 if service in undetermined else 0}
                    for service in json.loads(argv[2])
                }
            )
        )
        return {"pid": 1, "reused": False}


# meiva:note
# The onboarding step probes in the home the context names, stamps the record with the context's
# clock and names the probe's children by the instant it asked that clock: all three differ from what
# this test process would read for itself.
def test_the_onboarding_step_acts_on_the_context_home_and_clock(
    ctx: ConvergeCtx, unit_home: Path
) -> None:
    helper = _PreflightHelper()
    instant = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)
    mac = replace(
        _mac(ctx, unit_home, serve(cast("PermissionsHelperClient", helper))),
        clock=clock_at(instant),
    )

    converge_host.ensure_permissions_onboarding(mac)

    record = gate.read_record(gate.record_path(unit_home))
    assert record is not None
    assert record.checked_at == instant
    workdir = unit_home / "permissions-onboarding"
    assert set(helper.cwds) == {str(workdir)}
    assert (workdir / "preflight-converge-20261017T080000000000-probe.results.json").exists()
    assert helper.folders == [str(mac.session.user_home / FOLDER_PATHS["desktop-folder"])]
    forwarded = onboarding.gate_host(
        home=unit_home,
        user_home=mac.session.user_home,
        environ=mac.read_config().process_environment(),
        helper=None,
        timers=mac.timers,
    ).env
    from_the_hosts_launch = onboarding.gate_host(
        home=unit_home,
        user_home=mac.session.user_home,
        environ=mac.host.environ,
        helper=None,
        timers=mac.timers,
    ).env
    assert helper.envs[0] == forwarded
    assert forwarded != from_the_hosts_launch


# meiva:note
# The first pass over a home prints the whole checklist; a later one, over the record the first
# wrote, prints the one-line state and where to ask. The step finds the record under the home the
# context names.
def test_the_onboarding_step_reports_in_full_only_on_the_first_pass(
    ctx: ConvergeCtx, unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = _PreflightHelper()
    mac = replace(
        _mac(ctx, unit_home, serve(cast("PermissionsHelperClient", helper))),
        clock=clock_at(datetime(2026, 10, 17, 8, 0, tzinfo=UTC)),
    )

    converge_host.ensure_permissions_onboarding(mac)
    first = capsys.readouterr().err
    converge_host.ensure_permissions_onboarding(mac)
    second = capsys.readouterr().err

    later = "with someone at the machine to request what is still askable"
    assert later not in first
    assert later in second
