# meiva:note
# Converge browser step: what it does on a capable, an incapable and a switched-off host.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from cli.commands.converge.plans.browser_plan import (
    BrowserDisabled,
    BrowserIncapable,
    BrowserNotApplicable,
    OfferProfileSeed,
    plan_browser,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host as cv
from cli.commands.lifecycle.tests.converge_host.context_inputs import capable_mac

_NO_DISPLAY = "no display (headless server)"
_NO_CHROME = "no Chrome (install it or set MEIVA_CHROME_BINARY)"
_NO_NPX = "no npx (install Node.js for chrome-devtools-mcp)"


# meiva:note
# --- the decision (pure) ------------------------------------------------------------


def test_a_switched_off_browser_is_not_probed() -> None:
    assert plan_browser(enabled=False, incapability=None, interactive=True) == BrowserDisabled()
    assert plan_browser(enabled=False, incapability=_NO_NPX, interactive=True) == BrowserDisabled()


# meiva:note
# Watchdog respawns and rollout-driven starts have no terminal, so they never block.
@pytest.mark.parametrize("interactive", [True, False])
def test_a_capable_host_offers_the_profile_seed_to_a_person_at_the_terminal_only(
    interactive: bool,
) -> None:
    plan = plan_browser(enabled=True, incapability=None, interactive=interactive)

    assert plan == OfferProfileSeed(interactive=interactive)


def test_a_headless_host_gets_an_informational_notice_not_a_warning() -> None:
    plan = plan_browser(enabled=True, incapability=_NO_DISPLAY, interactive=True)

    assert plan == BrowserNotApplicable(_NO_DISPLAY)


@pytest.mark.parametrize("reason", [_NO_CHROME, _NO_NPX])
def test_a_repairable_gap_is_a_warning_and_never_offers_the_profile_seed(reason: str) -> None:
    plan = plan_browser(enabled=True, incapability=reason, interactive=True)

    assert plan == BrowserIncapable(reason)


# meiva:note
# --- the step on a host -------------------------------------------------------------


# meiva:note
# An agent-runner mac with the browser on and a configured Chrome, with or without npx.
def _browser_ctx(ctx: ConvergeCtx, *, npx: bool) -> ConvergeCtx:
    mac = replace(ctx, host=capable_mac(programs={"npx": "/usr/bin/npx"} if npx else None))
    mac.config.set_field("browser_enabled", True)
    mac.config.set_field("chrome_binary", "/Applications/Chrome")
    return mac


def test_a_capable_host_runs_the_preflight_quietly(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    cv.ensure_browser(_browser_ctx(ctx, npx=True))

    assert capsys.readouterr().err == ""


# meiva:note
# Pytest's real captured stdin refuses reads. Reaching the prompt proves the production
# root used this session's daily profile, rather than the process HOME or an inert spy.
def test_profile_offer_uses_the_captured_session_home(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str], tmp_path: Path, default_home: Path
) -> None:
    source = tmp_path / "captured-session" / "Library/Application Support/Google/Chrome"
    source.mkdir(parents=True)
    ready = _browser_ctx(ctx, npx=True)
    session = replace(ready.session, user_home=tmp_path / "captured-session", interactive=True)
    ready = replace(ready, session=session)
    assert session.user_home != default_home

    with pytest.raises(OSError, match="reading from stdin"):
        cv.ensure_browser(ready)

    assert str(source) in capsys.readouterr().out


# meiva:note
# A repairable dependency gap keeps converge's existing warn-not-raise path.
def test_enabled_with_missing_npx_prints_the_repair_box_and_does_not_raise(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    cv.ensure_browser(_browser_ctx(ctx, npx=False))

    err = capsys.readouterr().err
    assert "Install Node.js" in err
    assert "meiva-browser will not start on this host" in err


def test_a_switched_off_browser_prints_nothing(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    off = replace(ctx, host=capable_mac())
    off.config.set_field("browser_enabled", False)

    cv.ensure_browser(off)

    assert capsys.readouterr().err == ""


def test_steps_read_their_context_owner_and_later_overlay(
    ctx: ConvergeCtx, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _browser_ctx(ctx, npx=False)
    second_config = snapshot_process_config()
    second = replace(first, config=second_config, machine=ConfiguredMachineIdentity(second_config))
    first.config.set_field("browser_enabled", False)
    second.config.set_field("browser_enabled", True)
    second.config.set_field("chrome_binary", "/Applications/Chrome")

    cv.ensure_browser(first)
    # meiva:note
    # its own owner says the browser is off
    assert capsys.readouterr().err == ""
    cv.ensure_browser(second)
    # meiva:note
    # another owner says it is on
    assert "Install Node.js" in capsys.readouterr().err
    assert first.config is not second.config

    # meiva:note
    # a later overlay on the first owner
    first.config.set_field("browser_enabled", True)
    cv.ensure_browser(first)
    assert "Install Node.js" in capsys.readouterr().err


def test_context_read_failure_keeps_the_original_error(ctx: ConvergeCtx) -> None:
    error = RuntimeError("context configuration is unavailable")

    class FailedBoot(ConfigBoot):
        def read_process_environment(self) -> None:
            raise error

    failing = replace(ctx, config=FailedBoot())
    with pytest.raises(RuntimeError) as caught:
        cv.ensure_browser(failing)
    assert caught.value is error


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "browser capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True


def test_plugin_config_images_step_agent_runner_only() -> None:
    """Plugin config images are read only by agent processes (agent-runners); the
    step must not run on a gateway."""
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "plugin config images")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
