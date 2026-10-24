# meiva:note
# The ordered preparation of a cold start, and the checks every admitted start performs.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.start.cold_start_plan import (
    ColdStep,
    MigrationAuthority,
    SchemaCheck,
    migration_authority,
    plan_cold_start,
    plan_schema_checks,
)


def test_a_gateway_prepares_converge_storage_migrations_pooler_schema_then_extensions() -> None:
    assert plan_cold_start({"gateway"}) == (
        ColdStep.CONVERGE,
        ColdStep.DATA_PLANE,
        ColdStep.GATEWAY_SCHEMA,
        ColdStep.MIGRATIONS,
        ColdStep.POOLER,
        ColdStep.SCHEMA_CHECK,
        ColdStep.ADOPT_EXTENSIONS,
        ColdStep.MATERIALIZE_EXTENSIONS,
    )


def test_a_runner_only_host_skips_local_storage_and_the_pooler() -> None:
    plan = plan_cold_start({"agent-runner"})

    assert plan == (
        ColdStep.CONVERGE,
        ColdStep.SKIP_LOCAL_SERVICES,
        ColdStep.MIGRATIONS,
        ColdStep.SCHEMA_CHECK,
        ColdStep.ADOPT_EXTENSIONS,
        ColdStep.MATERIALIZE_EXTENSIONS,
    )
    assert ColdStep.DATA_PLANE not in plan and ColdStep.POOLER not in plan


def test_a_box_serving_both_roles_brings_up_storage() -> None:
    assert plan_cold_start({"gateway", "agent-runner"}) == plan_cold_start({"gateway"})


@pytest.mark.parametrize("roles", [{"gateway"}, {"agent-runner"}], ids=["gateway", "runner"])
def test_host_dependencies_and_converge_precede_every_other_step(roles: set[str]) -> None:
    assert plan_cold_start(roles)[0] is ColdStep.CONVERGE


# meiva:note
# Materialization reads the cluster registry, which exists only once pending migrations
# applied and the schema was checked current: the rollout that creates the extensions table
# must not read it before it exists. Adoption precedes materialization so one pass leaves the
# machine and the cluster agreeing.
@pytest.mark.parametrize("roles", [{"gateway"}, {"agent-runner"}], ids=["gateway", "runner"])
def test_extensions_land_after_migrations_and_the_schema_check(roles: set[str]) -> None:
    plan = plan_cold_start(roles)

    assert plan.index(ColdStep.MIGRATIONS) < plan.index(ColdStep.ADOPT_EXTENSIONS)
    assert plan.index(ColdStep.SCHEMA_CHECK) < plan.index(ColdStep.ADOPT_EXTENSIONS)
    assert plan.index(ColdStep.ADOPT_EXTENSIONS) < plan.index(ColdStep.MATERIALIZE_EXTENSIONS)


def test_storage_schema_migrations_and_the_pooler_come_in_that_order() -> None:
    plan = plan_cold_start({"gateway"})

    ordered = [
        ColdStep.DATA_PLANE,
        ColdStep.GATEWAY_SCHEMA,
        ColdStep.MIGRATIONS,
        ColdStep.POOLER,
    ]
    assert [step for step in plan if step in ordered] == ordered


def test_a_cold_start_never_runs_a_check_that_only_a_live_root_needs() -> None:
    assert plan_schema_checks(live=False) == (SchemaCheck.MEIVA,)


def test_a_live_root_is_only_inspected_and_its_checkpoint_schema_verified() -> None:
    assert plan_schema_checks(live=True) == (SchemaCheck.MEIVA, SchemaCheck.CHECKPOINT)


def test_a_plane_this_home_owns_migrates_over_its_owner_socket() -> None:
    assert migration_authority(remote_plane=False) is MigrationAuthority.OWNER_SOCKET


def test_a_remote_managed_plane_never_gets_a_local_owner_authority() -> None:
    assert migration_authority(remote_plane=True) is MigrationAuthority.ACCESS_URL
