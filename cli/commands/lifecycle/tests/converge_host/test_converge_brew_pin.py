from __future__ import annotations

from dataclasses import replace

import pytest

import cli.commands.converge.brew_pin as cbp
import cli.commands.lifecycle.converge_host as cv
from base.host import brew_pin
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx

EXPECTED_PINNED_FORMULAE = frozenset(
    {
        "ca-certificates",
        "cloudflared",
        "grafana",
        "json-c",
        "node",
        "openssl@3",
        "pgbouncer",
        "pgvector",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "uv",
    }
)


def _brew(pinned: set[str], installed: set[str] | None = None) -> Host:
    installed_formulae = set(EXPECTED_PINNED_FORMULAE) if installed is None else installed
    commands = FakeCommands()
    commands.on(["brew", "list", "--pinned"], Reply("\n".join(sorted(pinned))))
    commands.on(["brew", "list", "--formula"], Reply("\n".join(sorted(installed_formulae))))
    return fake_host(commands, platform=DARWIN_ARM64)


def test_pinned_formula_manifest_is_exact() -> None:
    assert brew_pin.PINNED_BREW_FORMULAE == EXPECTED_PINNED_FORMULAE


def test_step_is_registered_after_firewall_for_both_roles() -> None:
    steps = cv.CONVERGE_STEPS
    brew_pin_index = next(i for i, step in enumerate(steps) if step.apply is cbp.ensure_brew_pin)
    firewall_index = next(
        i for i, step in enumerate(steps) if step.apply.__name__ == "ensure_firewall_allowlist"
    )
    step = steps[brew_pin_index]
    assert brew_pin_index > firewall_index
    assert step.roles == ALL_ROLES
    assert step.requires_unit_config is False


def test_all_formulae_pinned_is_silent(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    host = _brew(set(EXPECTED_PINNED_FORMULAE))

    cbp.ensure_brew_pin(replace(ctx, host=host))

    assert capsys.readouterr().err == ""


def test_missing_formula_warns_with_manual_repin_command(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    host = _brew(set(EXPECTED_PINNED_FORMULAE - {"redis@8.2"}))

    cbp.ensure_brew_pin(replace(ctx, host=host))

    warning = capsys.readouterr().err
    assert warning.startswith("  ! brew-pin:")
    assert "redis@8.2" in warning
    assert "brew pin redis@8.2" in warning


def test_uninstalled_manifest_formula_is_not_flagged(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    installed = set(EXPECTED_PINNED_FORMULAE - {"grafana"})
    host = _brew(installed, installed=installed)

    cbp.ensure_brew_pin(replace(ctx, host=host))

    assert capsys.readouterr().err == ""


def test_installed_but_unpinned_formula_still_warns(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    pinned = set(EXPECTED_PINNED_FORMULAE - {"grafana"})
    host = _brew(pinned, installed=set(EXPECTED_PINNED_FORMULAE))

    cbp.ensure_brew_pin(replace(ctx, host=host))

    warning = capsys.readouterr().err
    assert "grafana" in warning
    assert "brew pin grafana" in warning


def test_brew_absent_says_the_check_did_not_run(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    commands = FakeCommands()
    commands.on("brew", FileNotFoundError("brew"), repeat=True)
    host = fake_host(commands, platform=DARWIN_ARM64)

    cbp.ensure_brew_pin(replace(ctx, host=host))

    assert "brew-pin: check skipped" in capsys.readouterr().err


def test_a_failed_brew_query_does_not_read_as_all_pinned(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
    loguru_records: LogRecords,
) -> None:
    commands = FakeCommands()
    commands.on(["brew", "list", "--pinned"], Reply(returncode=1, stderr="boom"), repeat=True)
    host = fake_host(commands, platform=DARWIN_ARM64)

    cbp.ensure_brew_pin(replace(ctx, host=host))

    assert "brew-pin: check skipped" in capsys.readouterr().err
    [event] = loguru_records.events("brew_pin")
    assert event["level"].name == "WARNING"
    assert event["extra"]["rc"] == 1


def test_non_macos_is_silent_without_calling_brew(
    capsys: pytest.CaptureFixture[str],
    ctx: ConvergeCtx,
) -> None:
    # meiva:note
    # The fake host answers no command: a call to brew would fail the step.
    host = fake_host(FakeCommands(), platform=LINUX_X86)

    cbp.ensure_brew_pin(replace(ctx, host=host))

    assert capsys.readouterr().err == ""
