# meiva:note
# Which database and API delivery a service class gets, as the kind the pure functions return.

from __future__ import annotations

from collections.abc import Callable

import pytest

from cli.commands.data_plane.instance.launch_delivery import (
    Delivery,
    api_delivery_kind,
    db_delivery_kind,
)

_Decide = Callable[[str, bool, bool], Delivery]


def _db(cls: str, local: bool, remote: bool) -> Delivery:
    return db_delivery_kind(cls, config_source_is_local=local, remote_plane=remote)


def _api(cls: str, local: bool, remote: bool) -> Delivery:
    return api_delivery_kind(cls, config_source_is_local=local, remote_plane=remote)


@pytest.mark.parametrize("decide", [_db, _api])
def test_a_gateway_home_delivers_the_active_generation(decide: _Decide) -> None:
    assert decide("gateway", True, False) is Delivery.GENERATION


@pytest.mark.parametrize("decide", [_db, _api])
def test_a_remote_managed_plane_delivers_nothing(decide: _Decide) -> None:
    assert decide("runner", True, True) is Delivery.NONE


@pytest.mark.parametrize("decide", [_db, _api])
def test_a_pure_runner_delivers_its_installed_capability(decide: _Decide) -> None:
    assert decide("runner", False, False) is Delivery.UNIT_CAPABILITY


def test_a_pure_runner_cannot_launch_a_gateway_class_database_service() -> None:
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class database service"):
        _db("gateway", False, False)


def test_a_pure_runner_cannot_launch_a_gateway_class_service() -> None:
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class service"):
        _api("gateway", False, False)
