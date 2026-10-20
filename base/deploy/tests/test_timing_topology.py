from __future__ import annotations

import pytest

import base.deploy.progress_timeout as deploy
import base.native_process.stop_timing as stop
from base import config
from base.deploy.timing import CLOCKS, CONSTRAINTS, assert_clock_lattice, validate_clock_lattice
from base.host.db_authority import unit


def test_default_lattice_holds() -> None:
    failures = validate_clock_lattice()
    assert failures == [], "lattice violated:\n  " + "\n  ".join(failures)


def test_checker_catches_lt_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy, "GATEWAY_PREFLIGHT_BUDGET_S", deploy.NO_PROGRESS_TIMEOUT_S + 10)
    failures = validate_clock_lattice()
    assert any("GATEWAY_PREFLIGHT_BUDGET_S < NO_PROGRESS_TIMEOUT_S" in f for f in failures)


def test_checker_catches_derived_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.daemon, "wedged_agent_inbound_age_seconds", 500.0)
    failures = validate_clock_lattice()
    assert any("WEDGED_AGE_SEC >= EXEC_NODE_TIMEOUT_S" in f for f in failures)


def test_a_default_bundle_lifetime_past_the_cap_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "UNIT_BUNDLE_TTL_S", unit.UNIT_BUNDLE_MAX_TTL_S + 1)
    failures = validate_clock_lattice()
    assert any("UNIT_BUNDLE_TTL_S <= UNIT_BUNDLE_MAX_TTL_S" in f for f in failures)


# meiva:note
# The drain waits for turns whose restart interrupts them after the grace: the lattice states
# that it exceeds the grace, the next poll and one unwind, and the defaults hold it.
def test_the_graceful_exit_drain_is_declared_to_outlast_a_restart_interrupt() -> None:
    [declared] = [
        c
        for c in CONSTRAINTS
        if c.rhs == "HOST_GRACEFUL_EXIT_DRAIN_S" and "RESTART_INTERRUPT_GRACE_S" in c.lhs
    ]
    assert declared.kind == "<"
    assert declared.lhs == "RESTART_INTERRUPT_GRACE_S + INTERRUPT_POLL_S + CANCEL_UNWIND_TIMEOUT_S"
    assert (
        stop.RESTART_INTERRUPT_GRACE_S + stop.INTERRUPT_POLL_S + stop.CANCEL_UNWIND_TIMEOUT_S
        < stop.HOST_GRACEFUL_EXIT_DRAIN_S
    )


def test_the_restart_interrupt_clocks_are_the_stop_family_values() -> None:
    assert CLOCKS["RESTART_INTERRUPT_GRACE_S"].get() == stop.RESTART_INTERRUPT_GRACE_S
    assert CLOCKS["INTERRUPT_POLL_S"].get() == stop.INTERRUPT_POLL_S
    assert {CLOCKS["RESTART_INTERRUPT_GRACE_S"].family, CLOCKS["INTERRUPT_POLL_S"].family} == {
        "stop"
    }


def test_assert_clock_lattice_raises_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.deploy.timing import ClockLatticeError

    monkeypatch.setattr(deploy, "GATEWAY_PREFLIGHT_BUDGET_S", deploy.NO_PROGRESS_TIMEOUT_S + 10)
    with pytest.raises(ClockLatticeError):
        assert_clock_lattice()


def test_every_deploy_family_clock_is_registered() -> None:
    registered = set(CLOCKS)
    for name in dir(deploy):
        if name.startswith("_"):
            continue
        value = getattr(deploy, name)
        if isinstance(value, (int, float)):
            assert name in registered, (
                f"{name} defined in progress_timeout but not registered in CLOCKS"
            )


def test_every_constraint_references_registered_clocks() -> None:
    registered = set(CLOCKS)
    for c in CONSTRAINTS:
        for expr in (c.lhs, c.rhs):
            for token in expr.replace(" + ", " ").replace(" * ", " ").split():
                if token.isdigit():
                    continue
                assert token in registered, (
                    f"constraint references unknown clock {token!r} in {c.lhs} {c.kind} {c.rhs}"
                )


def test_constraint_kinds_are_known() -> None:
    for c in CONSTRAINTS:
        assert c.kind in {"<", "<=", "==", ">="}, f"unknown constraint kind {c.kind!r}"
