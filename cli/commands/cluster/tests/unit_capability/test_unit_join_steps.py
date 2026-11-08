# meiva:note
# The steps of a remote unit's join and the input of each, decided from values.
#
# Which credential a join presents, what the install is handed once the gateway answered and
# what is said when it landed are asserted on the plan; that the shell opens, dials, installs
# and deletes accordingly is `test_join_gateway.py`.

from __future__ import annotations

import pytest

from cli.commands.cluster.plans.unit_join_steps import (
    CarriedBundle,
    InstallBundle,
    InstalledCapability,
    installed_line,
    plan_credential,
    plan_install,
)

_KEY_ENV = "MEIVA_DB_CAPABILITY_KEY"
_SERVED = {
    "MEIVA_DB_URL": "postgresql://meiva@10.0.0.7:6433/meiva",
    "MEIVA_REDIS_URL": "redis://10.0.0.7:6379/0",
}


def test_a_join_that_carries_a_bundle_presents_the_bundles_credential() -> None:
    assert plan_credential(
        "/in/mini.bundle", key_supplied=True, env_name=_KEY_ENV
    ) == CarriedBundle("/in/mini.bundle")


@pytest.mark.parametrize("transport_key", ["", "key"])
def test_a_join_without_a_bundle_presents_the_installed_capability_whatever_key_was_exported(
    transport_key: str,
) -> None:
    assert plan_credential(None, key_supplied=bool(transport_key), env_name=_KEY_ENV) == (
        InstalledCapability()
    )


def test_a_bundle_is_planned_as_carried_bundle() -> None:
    assert plan_credential("/in/mini.bundle") == CarriedBundle("/in/mini.bundle")


def test_the_install_is_handed_this_machine_the_endpoint_served_now_and_the_bundle_file() -> None:
    step = plan_install(CarriedBundle("/in/mini.bundle"), _SERVED, {"MEIVA_MACHINE_NAME": "mini"})

    assert step == InstallBundle(
        machine="mini",
        served_endpoint="postgresql://meiva@10.0.0.7:6433/meiva",
        consume_path="/in/mini.bundle",
    )


@pytest.mark.parametrize("identity", [{}, {"MEIVA_MACHINE_NAME": ""}])
def test_a_bundle_cannot_be_installed_by_a_unit_with_no_machine_name(
    identity: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="bound to a machine name"):
        plan_install(CarriedBundle("/in/mini.bundle"), _SERVED, identity)


def test_a_gateway_that_served_no_database_endpoint_leaves_nothing_to_install_against() -> None:
    with pytest.raises(ValueError, match="serves no MEIVA_DB_URL"):
        plan_install(CarriedBundle("/in/mini.bundle"), {}, {"MEIVA_MACHINE_NAME": "mini"})


def test_the_line_said_after_an_install_names_the_generation_and_the_consumed_bundle() -> None:
    assert installed_line(0, "/in/mini.bundle") == (
        "  ✓ database capability installed: write generation 0; bundle /in/mini.bundle consumed"
    )
