from __future__ import annotations

import pytest

import base.deploy.progress_timeout as deploy
import base.native_process.stop_timing as stop
from base.config.domains.daemon.settings import DaemonSettings
from base.deploy.timing import (
    CLOCKS,
    CONSTRAINTS,
    assert_clock_lattice,
    clock_values,
    validate_clock_lattice,
)
from base.host.db_authority import unit


def test_default_lattice_holds() -> None:
    failures = validate_clock_lattice(clock_values())
    assert failures == [], "lattice violated:\n  " + "\n  ".join(failures)


def test_checker_catches_lt_violation() -> None:
    values = clock_values()
    values["GATEWAY_PREFLIGHT_BUDGET_S"] = deploy.NO_PROGRESS_TIMEOUT_S + 10
    failures = validate_clock_lattice(values)
    assert any("GATEWAY_PREFLIGHT_BUDGET_S < NO_PROGRESS_TIMEOUT_S" in f for f in failures)


def test_checker_catches_derived_violation() -> None:
    values = clock_values()
    values["WEDGED_AGE_SEC"] = 500.0
    failures = validate_clock_lattice(values)
    assert any("WEDGED_AGE_SEC >= EXEC_NODE_TIMEOUT_S" in f for f in failures)


def test_a_default_bundle_lifetime_past_the_cap_is_reported() -> None:
    values = clock_values()
    values["UNIT_BUNDLE_TTL_S"] = unit.UNIT_BUNDLE_MAX_TTL_S + 1
    failures = validate_clock_lattice(values)
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


def test_assert_clock_lattice_raises_on_violation() -> None:
    from base.deploy.timing import ClockLatticeError

    values = clock_values()
    values["GATEWAY_PREFLIGHT_BUDGET_S"] = deploy.NO_PROGRESS_TIMEOUT_S + 10
    with pytest.raises(ClockLatticeError):
        assert_clock_lattice(values)


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


@pytest.mark.parametrize("missing", [True, False])
def test_snapshot_rejects_missing_or_unknown_clock_names(missing: bool) -> None:
    values = clock_values()
    if missing:
        del values["GATEWAY_PREFLIGHT_BUDGET_S"]
    else:
        values["UNKNOWN_CLOCK"] = 1.0
    with pytest.raises(ValueError, match="exactly the registered clock names"):
        validate_clock_lattice(values)


# meiva:note
# The held path's retry budget (#5468) must stay under both neighbours: a wake spending it
# can never look stalled, and it finishes long before the reaper's grace, so the retry and
# the reaper never race for the same row. The declared backoff sequence must also fit the
# budget it declares, or the third attempt would only ever be cut short.
def test_the_handoff_settle_budget_is_bounded_by_the_stall_and_reap_clocks() -> None:
    declared = {c.rhs: c for c in CONSTRAINTS if c.lhs == "HANDOFF_SETTLE_RETRY_BUDGET_S"}
    assert set(declared) == {"NO_PROGRESS_TIMEOUT_S", "CORPSE_REAP_GRACE_S"}
    assert all(c.kind == "<" for c in declared.values())
    assert CLOCKS["HANDOFF_SETTLE_RETRY_BUDGET_S"].get() == deploy.HANDOFF_SETTLE_RETRY_BUDGET_S
    assert CLOCKS["HANDOFF_SETTLE_RETRY_INTERVAL_S"].get() == deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S
    assert deploy.HANDOFF_SETTLE_RETRY_BUDGET_S < deploy.NO_PROGRESS_TIMEOUT_S
    assert deploy.HANDOFF_SETTLE_RETRY_BUDGET_S < deploy.CORPSE_REAP_GRACE_S
    assert (
        deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S + 3 * deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S
        < deploy.HANDOFF_SETTLE_RETRY_BUDGET_S
    )


# meiva:note
# The host root judges the values it will live by: an operator's settle budget reaches the lattice
# through the parameter, so an override that inverts one of the held path's orderings is refused at
# boot rather than at the failing wake.
def test_an_operator_settle_budget_is_what_the_lattice_checks() -> None:
    values = clock_values(handoff_settle_budget_s=deploy.NO_PROGRESS_TIMEOUT_S)
    assert values["HANDOFF_SETTLE_RETRY_BUDGET_S"] == deploy.NO_PROGRESS_TIMEOUT_S
    assert any(
        "HANDOFF_SETTLE_RETRY_BUDGET_S < NO_PROGRESS_TIMEOUT_S" in f
        for f in validate_clock_lattice(values)
    )


# meiva:note
# The configuration owns the running value and this family owns its two bounds; only this module may
# read both, so the agreement is proved here. The default is seeded from the one definition above,
# and a configured value at either bound is what the host root refuses at boot.
def test_the_configuration_seeds_the_budget_from_the_deploy_family() -> None:
    assert DaemonSettings().held_settle_budget_s == deploy.HANDOFF_SETTLE_RETRY_BUDGET_S


@pytest.mark.parametrize(
    ("bound", "name"),
    [
        (deploy.NO_PROGRESS_TIMEOUT_S, "NO_PROGRESS_TIMEOUT_S"),
        (deploy.CORPSE_REAP_GRACE_S, "CORPSE_REAP_GRACE_S"),
    ],
)
def test_a_configured_settle_budget_at_a_deploy_bound_fails_the_lattice(
    bound: float, name: str
) -> None:
    loaded = DaemonSettings(MEIVA_HOST_HELD_SETTLE_BUDGET_S=bound).held_settle_budget_s
    failures = validate_clock_lattice(clock_values(handoff_settle_budget_s=loaded))
    assert [f for f in failures if f"HANDOFF_SETTLE_RETRY_BUDGET_S < {name}" in f]


# meiva:note
# The same family's floor: the retry pays one interval and then three times it before the third
# attempt, so a budget below that sum cannot buy the three attempts the budget is declared to pay
# for. Declared here and refused at boot, like the two ceilings.
def test_the_settle_budget_declares_the_floor_its_attempts_need() -> None:
    [floor] = [c for c in CONSTRAINTS if c.rhs == "HANDOFF_SETTLE_RETRY_BUDGET_S"]
    assert floor.kind == "<"
    assert floor.lhs == "HANDOFF_SETTLE_RETRY_INTERVAL_S + 3 * HANDOFF_SETTLE_RETRY_INTERVAL_S"
    assert (
        deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S + 3 * deploy.HANDOFF_SETTLE_RETRY_INTERVAL_S
        < deploy.HANDOFF_SETTLE_RETRY_BUDGET_S
    )


@pytest.mark.parametrize("budget", [1.0, 1.9])
def test_a_budget_too_small_for_three_attempts_fails_the_lattice(budget: float) -> None:
    failures = validate_clock_lattice(clock_values(handoff_settle_budget_s=budget))
    assert [f for f in failures if "< HANDOFF_SETTLE_RETRY_BUDGET_S:" in f]
