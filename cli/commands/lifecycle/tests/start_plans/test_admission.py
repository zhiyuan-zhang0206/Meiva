# meiva:note
# Whether a start may go on: the refusals, the identity line and the gateway probe.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.start.admission import (
    Refusal,
    capability_refusal,
    guard_refusal,
    probes_gateway,
    roles_line,
)


def test_a_home_being_destroyed_raises_instead_of_reporting() -> None:
    with pytest.raises(RuntimeError, match="home is being destroyed or detached"):
        guard_refusal(destroy_intent=True, checkout_error=None)


def test_destruction_outranks_a_checkout_refusal() -> None:
    with pytest.raises(RuntimeError, match="startup refused"):
        guard_refusal(destroy_intent=True, checkout_error="a disposable checkout")


def test_a_disposable_checkout_is_refused_with_the_rules_own_words() -> None:
    assert guard_refusal(
        destroy_intent=False, checkout_error="not this home's checkout"
    ) == Refusal("✗ not this home's checkout")


def test_a_home_with_nothing_wrong_is_not_refused() -> None:
    assert guard_refusal(destroy_intent=False, checkout_error=None) is None


def test_capabilities_that_differ_from_the_recorded_ones_are_refused() -> None:
    refusal = capability_refusal({"gateway", "agent-runner"}, ["agent-runner"])

    assert refusal == Refusal(
        "\n✗ this home's capabilities (agent-runner,gateway) differ from the ones "
        "`meiva init` recorded (agent-runner); capabilities are fixed when a home is initialized"
    )


@pytest.mark.parametrize(
    ("roles", "recorded"),
    [
        ({"agent-runner", "gateway"}, ["agent-runner", "gateway"]),
        ({"gateway"}, ["gateway"]),
        ({"gateway"}, None),
    ],
    ids=["same set", "single role", "nothing recorded"],
)
def test_the_recorded_capabilities_or_none_recorded_do_not_refuse(
    roles: set[str], recorded: list[str] | None
) -> None:
    assert capability_refusal(roles, recorded) is None


def test_the_identity_line_names_the_sorted_roles_and_the_machine() -> None:
    assert roles_line({"gateway", "agent-runner"}, "mini") == (
        "\n→ roles = agent-runner,gateway, machine = mini"
    )


@pytest.mark.parametrize(
    ("roles", "probes"),
    [
        ({"agent-runner"}, True),
        ({"gateway"}, False),
        ({"gateway", "agent-runner"}, False),
        ({"observability-station"}, False),
    ],
    ids=["pure runner", "pure gateway", "co-located box", "station only"],
)
def test_only_a_pure_agent_runner_probes_the_gateway(roles: set[str], probes: bool) -> None:
    assert probes_gateway(roles) is probes
