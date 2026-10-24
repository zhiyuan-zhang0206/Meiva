# meiva:note
# Which services the health probe may certify and which of them failed, decided from values.
#
# Every intended service needs positive evidence; a gated-out or never-launched one does not
# count against the cluster, and a roster or launch record the probe cannot read certifies
# nothing even when the gateway responds.

from __future__ import annotations

from cli.commands.cluster.plans.service_probe_plan import (
    ROSTER_UNAVAILABLE,
    Launched,
    LaunchUnreadable,
    NeverLaunched,
    ProbeOutcome,
    RosterGate,
    Unavailable,
    bridge_failure,
    expects_redis_bridge,
    failing_services,
    launch_scope,
    sessions_to_probe,
)


def test_a_launched_record_scopes_the_probe() -> None:
    launched = Launched(frozenset({"gateway"}))

    assert launch_scope(launched) is launched


def test_a_unit_no_start_has_launched_has_no_inventory_to_certify() -> None:
    assert launch_scope(NeverLaunched()) == Unavailable(
        "launched services unavailable (no start has launched this unit)"
    )


def test_an_unreadable_launch_record_certifies_nothing_and_says_why() -> None:
    assert launch_scope(LaunchUnreadable("corrupt manifest")) == Unavailable(
        "launched services unavailable (corrupt manifest)"
    )


def test_an_unresolved_roster_is_a_named_failure() -> None:
    assert ROSTER_UNAVAILABLE == "service roster unavailable"


def test_only_services_the_gate_keeps_and_the_last_start_launched_are_probed() -> None:
    roster = [
        RosterGate("gateway", None),
        RosterGate("frontend", None),
        RosterGate("browser", "disabled (MEIVA_BROWSER_ENABLED off)"),
        RosterGate("ops", None),
    ]
    launched = Launched(frozenset({"gateway", "browser", "ops"}))

    assert sessions_to_probe(launched, roster) == ("gateway", "ops")


def test_a_roster_gate_reason_wins_over_having_been_launched() -> None:
    launched = Launched(frozenset({"browser"}))

    assert sessions_to_probe(launched, [RosterGate("browser", "no display")]) == ()


def test_only_positive_evidence_is_healthy() -> None:
    outcomes = [
        ProbeOutcome("gateway", True, ""),
        ProbeOutcome("frontend", False, ""),
        ProbeOutcome("browser-mcp", None, ""),
    ]

    assert failing_services(outcomes) == ("frontend", "browser-mcp")


def test_a_failing_probe_carries_the_fact_that_failed() -> None:
    # meiva:note
    # A wrong-home listener must be diagnosed distinctly from an absent listener: the
    # session name alone is the same for both and the second needs a different action.
    outcomes = [ProbeOutcome("ops", False, "home='/home/meiva/.meiva' != '/u/.meiva'")]

    assert failing_services(outcomes) == ("ops (home='/home/meiva/.meiva' != '/u/.meiva')",)


def test_nothing_failing_is_an_empty_tuple() -> None:
    assert failing_services([]) == ()
    assert failing_services([ProbeOutcome("gateway", True, "ready")]) == ()


def test_only_a_gateway_host_has_a_redis_bridge_to_certify() -> None:
    assert expects_redis_bridge(frozenset({"gateway"}))
    assert expects_redis_bridge(frozenset({"gateway", "agent-runner"}))
    assert not expects_redis_bridge(frozenset({"agent-runner"}))
    assert not expects_redis_bridge(None)


def test_a_relay_whose_ping_path_is_dead_is_reported_even_when_its_process_is_up() -> None:
    assert (
        bridge_failure(
            required=True, serving=False, endpoint="10.64.0.7:6380", detail="connection refused"
        )
        == "Redis bridge 10.64.0.7:6380 failed authenticated PING (connection refused)"
    )


def test_a_relay_without_an_endpoint_still_fails_without_the_address() -> None:
    assert bridge_failure(required=True, serving=False, endpoint=None, detail="refused") == (
        "Redis bridge failed authenticated PING (refused)"
    )


def test_a_serving_or_not_required_relay_is_healthy() -> None:
    assert bridge_failure(required=True, serving=True, endpoint="x", detail="PING ok") is None
    assert bridge_failure(required=False, serving=False, endpoint=None, detail="") is None
