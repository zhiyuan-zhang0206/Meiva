# meiva:note
# Which captured services a maintenance stop ends, in which order and how; and what Postgres'
# fast shutdown leaves for the legs after it.

from __future__ import annotations

import itertools

import pytest

from cli.commands.data_plane.maintenance.plan import (
    ShutdownRedis,
    StopPooler,
    StopPostgres,
    fast_shutdown_reserve_s,
    plan_stop,
)
from cli.commands.data_plane.stop_budget import postgres_fast_budget_s

_ALL = frozenset({"pgbouncer", "postgres", "redis"})


# meiva:note
# The pooler first so its clients are disconnected before Postgres goes down, Postgres second,
# and Redis last, whatever order the services were captured in.
def test_the_pooler_stops_before_postgres_and_postgres_before_redis() -> None:
    assert plan_stop(_ALL, save=True) == (StopPooler(), StopPostgres(), ShutdownRedis("SAVE"))


@pytest.mark.parametrize(
    "captured", [_ALL, frozenset({"redis"}), frozenset[str](), _ALL - {"postgres"}]
)
def test_only_the_captured_services_are_stopped(captured: frozenset[str]) -> None:
    steps = plan_stop(captured, save=True)

    assert {step.service for step in steps} == captured


def test_redis_saves_its_data_unless_the_caller_proved_a_final_snapshot() -> None:
    assert plan_stop(frozenset({"redis"}), save=True) == (ShutdownRedis("SAVE"),)
    assert plan_stop(frozenset({"redis"}), save=False) == (ShutdownRedis("NOSAVE"),)


def test_a_service_the_stop_does_not_know_is_a_bug_in_the_caller() -> None:
    with pytest.raises(ValueError, match="unknown data-plane service"):
        plan_stop(frozenset({"postgres", "memcached"}), save=True)


def test_every_subset_keeps_the_same_relative_order() -> None:
    order = ["pgbouncer", "postgres", "redis"]
    for size in range(4):
        for subset in itertools.combinations(order, size):
            steps = plan_stop(frozenset(subset), save=True)

            assert [step.service for step in steps] == list(subset)


def test_the_reserve_is_the_immediate_wait_the_kill_wait_and_the_save_wait() -> None:
    assert fast_shutdown_reserve_s(10.0, 3.0) == 23.0
    assert fast_shutdown_reserve_s(1.0, 1.0) == 3.0


# meiva:note
# The fast shutdown gets what is left minus the reserve, so a stuck fast shutdown cannot spend
# the whole shared budget; a stop with less time than the reserve gives it all of it.
def test_the_fast_shutdown_leaves_room_for_the_legs_after_it() -> None:
    reserve = fast_shutdown_reserve_s(10.0, 3.0)

    assert postgres_fast_budget_s(300.0, reserve) == 277.0
    assert postgres_fast_budget_s(20.0, reserve) == 20.0
    assert postgres_fast_budget_s(23.0, reserve) == 23.0
