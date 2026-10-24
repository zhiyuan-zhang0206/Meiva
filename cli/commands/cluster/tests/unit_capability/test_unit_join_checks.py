# meiva:note
# What a remote unit's join refuses, decided from the values it was given.
#
# A unit that holds the human cluster secret, or joins a remote gateway without an address the
# gateway can dial back, or is handed loopback data-plane URLs by a remote gateway, can never
# work; each is refused before anything is fetched or written.

from __future__ import annotations

import pytest

from cli.commands.cluster.plans.unit_join_checks import (
    JoinTarget,
    data_plane_urls,
    join_target,
    require_routable_data_plane,
    served_value,
)

_HOME = "/home/u/.meiva"


def test_a_unit_joining_a_remote_gateway_with_a_reachable_host_is_accepted() -> None:
    values = {"MEIVA_GATEWAY_URL": "http://10.0.0.7:8000", "MEIVA_MACHINE_HOST": "10.0.0.9"}

    assert join_target(values, _HOME, "10.0.0.7") == JoinTarget("http://10.0.0.7:8000", remote=True)


def test_a_unit_joining_a_gateway_on_its_own_box_needs_no_reachable_host() -> None:
    values = {"MEIVA_GATEWAY_URL": "http://127.0.0.1:8000"}

    assert join_target(values, _HOME, "127.0.0.1") == JoinTarget(
        "http://127.0.0.1:8000", remote=False
    )


@pytest.mark.parametrize("host", [None, "", "localhost", "127.0.0.1", "::1"])
def test_a_remote_gateway_cannot_dial_back_a_unit_without_a_reachable_host(
    host: str | None,
) -> None:
    values = {"MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"}
    if host is not None:
        values["MEIVA_MACHINE_HOST"] = host

    with pytest.raises(ValueError, match="requires a reachable --machine-host"):
        join_target(values, _HOME, "10.0.0.7")


def test_a_unit_never_holds_the_human_cluster_secret() -> None:
    values = {"MEIVA_GATEWAY_URL": "http://10.0.0.7:8000", "MEIVA_CLUSTER_SECRET": "x"}

    with pytest.raises(ValueError, match=r"records the human cluster secret") as refused:
        join_target(values, _HOME, "10.0.0.7")

    assert _HOME in str(refused.value)


def test_the_secret_refusal_comes_before_any_gateway_check() -> None:
    with pytest.raises(ValueError, match="human cluster secret"):
        join_target(
            {"MEIVA_CLUSTER_SECRET": "x", "MEIVA_GATEWAY_URL": "http://10.0.0.7"}, _HOME, "10.0.0.7"
        )


def test_a_unit_with_no_gateway_url_fails_fast() -> None:
    with pytest.raises(KeyError):
        join_target({}, _HOME, "")


@pytest.mark.parametrize("key", ["MEIVA_DB_URL", "MEIVA_REDIS_URL"])
def test_a_remote_gateway_that_serves_a_loopback_data_plane_host_is_refused(key: str) -> None:
    hosts = {"MEIVA_DB_URL": "10.0.0.7", "MEIVA_REDIS_URL": "10.0.0.7"}
    hosts[key] = "127.0.0.1"

    with pytest.raises(ValueError, match="loopback data-plane URLs"):
        require_routable_data_plane(
            JoinTarget("http://10.0.0.7:8000", remote=True), list(hosts.values())
        )


def test_a_remote_gateway_serving_routable_urls_and_a_local_gateway_serving_loopback_pass() -> None:
    require_routable_data_plane(
        JoinTarget("http://10.0.0.7:8000", remote=True), ["10.0.0.7", "10.0.0.7"]
    )
    require_routable_data_plane(
        JoinTarget("http://127.0.0.1:8000", remote=False), ["127.0.0.1", "127.0.0.1"]
    )


def test_the_data_plane_urls_a_gateway_serves_are_its_database_then_its_redis() -> None:
    served = {
        "MEIVA_REDIS_URL": "redis://10.0.0.7:6379/0",
        "MEIVA_DB_URL": "postgresql://meiva@10.0.0.7:6433/meiva",
        "MEIVA_EVENTS_CHANNEL": "meiva",
    }

    assert data_plane_urls(served) == (
        "postgresql://meiva@10.0.0.7:6433/meiva",
        "redis://10.0.0.7:6379/0",
    )


@pytest.mark.parametrize("missing", ["MEIVA_DB_URL", "MEIVA_REDIS_URL"])
def test_a_gateway_that_serves_no_data_plane_url_is_refused_by_the_key_it_lacks(
    missing: str,
) -> None:
    served = {"MEIVA_DB_URL": "postgresql://meiva@10.0.0.7/meiva", "MEIVA_REDIS_URL": "redis://x"}
    del served[missing]

    with pytest.raises(ValueError, match=f"serves no {missing}"):
        data_plane_urls(served)


def test_a_served_value_is_read_as_the_gateway_sent_it() -> None:
    assert served_value({"MEIVA_DB_URL": "postgresql://meiva@h/db"}, "MEIVA_DB_URL") == (
        "postgresql://meiva@h/db"
    )
