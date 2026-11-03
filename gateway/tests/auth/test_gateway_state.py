import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import State

from gateway.http.config import GatewayAppState, GatewayStateDep, app_state_as


class _SliceState(GatewayAppState):
    sentinel: str


def test_dependency_hands_a_handler_the_state_the_app_was_built_with() -> None:
    app = FastAPI()
    state = GatewayAppState()
    state.started_at = 12.5
    app.state = state

    @app.get("/started-at")
    def started_at(gateway: GatewayStateDep) -> float:
        return gateway.started_at

    assert TestClient(app).get("/started-at").json() == 12.5


def test_handler_reads_an_attribute_a_test_assigned_after_the_app_was_built() -> None:
    app = FastAPI()
    app.state = GatewayAppState()

    @app.get("/started-at")
    def started_at(gateway: GatewayStateDep) -> float:
        return gateway.started_at

    client = TestClient(app)
    app.state.started_at = 1.0
    assert client.get("/started-at").json() == 1.0
    app.state.started_at = 2.0
    assert client.get("/started-at").json() == 2.0


def test_state_of_another_class_is_refused_with_the_class_it_lacks() -> None:
    plain = FastAPI()
    assert isinstance(plain.state, State)
    with pytest.raises(TypeError, match="GatewayAppState"):
        app_state_as(plain, GatewayAppState)


def test_object_that_is_not_an_app_is_refused() -> None:
    with pytest.raises(TypeError, match="GatewayAppState"):
        app_state_as(None, GatewayAppState)


def test_directory_slice_is_reached_only_when_the_app_was_built_with_it() -> None:
    base_only = FastAPI()
    base_only.state = GatewayAppState()
    with pytest.raises(TypeError, match="_SliceState"):
        app_state_as(base_only, _SliceState)

    sliced = FastAPI()
    sliced.state = _SliceState()
    sliced.state.sentinel = "x"
    assert app_state_as(sliced, _SliceState).sentinel == "x"
    assert app_state_as(sliced, GatewayAppState) is sliced.state
