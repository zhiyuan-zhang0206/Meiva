from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from gateway.events.loki_query import LokiFilters, log_query
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.events.loki_stub import LokiStub, event_line

_URL = "/api/cluster/admin/events"
_LOG = ("telemetry", "log")


@contextmanager
def _loki(**fields: object) -> Generator[tuple[LokiStub, GatewayTestClient]]:
    with (
        LokiStub() as stub,
        gateway_test_client(fields={"telemetry_loki_url": stub.url, **fields}) as client,
    ):
        yield stub, client


def _seed(
    stub: LokiStub, *, msg: str = "hi", seconds_ago: float = 60.0, event_name: str = "log"
) -> None:
    ts = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    stub.script.lines.append(event_line(ts, event_name, attributes={"msg": msg}))


def _messages(body: dict[str, object]) -> list[str]:
    # meiva:note
    # The annotated shape is the one the untyped call returns.
    items: list[dict[str, dict[str, str]]] = body["items"]  # pyright: ignore[reportAssignmentType]
    return [item["payload"]["msg"] for item in items]


def _queries(stub: LokiStub) -> list[str]:
    return [q["query"] for q in stub.script.queries]


def test_returns_newest_first_in_the_row_shape() -> None:
    with _loki() as (stub, client):
        _seed(stub, msg="oldest", seconds_ago=300)
        _seed(stub, msg="newest", seconds_ago=10)
        response = client.get(f"{_URL}?limit=10")

    assert response.status_code == 200
    assert _messages(response.json()) == ["newest", "oldest"]
    assert set(response.json()["items"][0]) == {
        "id",
        "line_sha256",
        "ts",
        "agent_id",
        "level",
        "event",
        "payload",
    }


def test_the_filters_are_one_loki_query() -> None:
    with _loki() as (stub, client):
        client.get(f"{_URL}?agent_id=3&level=WARNING&event=spawn,terminate&grep=hello")
        client.get(f"{_URL}?service_only=true")

    assert _queries(stub) == [
        log_query(
            LokiFilters(
                agent_id=3,
                level_min="warning",
                event_names=("spawn", "terminate"),
                grep="hello",
                categories=_LOG,
            )
        ),
        log_query(LokiFilters(service_only=True, categories=_LOG)),
    ]


def test_since_bounds_the_window_in_both_forms() -> None:
    with _loki() as (stub, client):
        _seed(stub, msg="recent", seconds_ago=30)
        _seed(stub, msg="two hours old", seconds_ago=7200)
        relative = client.get(f"{_URL}?since=1h")
        absolute = client.get(f"{_URL}?since=2020-01-01T00:00:00Z")

    assert _messages(relative.json()) == ["recent"]
    assert _messages(absolute.json()) == ["recent", "two hours old"]


def test_the_default_window_is_the_last_day() -> None:
    with _loki() as (stub, client):
        _seed(stub, msg="inside", seconds_ago=23 * 3600)
        _seed(stub, msg="outside", seconds_ago=25 * 3600)
        response = client.get(_URL)

    assert _messages(response.json()) == ["inside"]


def test_an_explicit_limit_wins_and_the_default_comes_from_display_config() -> None:
    with _loki(cluster_events_default_limit=3) as (stub, client):
        for index in range(4):
            _seed(stub, msg=f"row {index}", seconds_ago=10 + index)
        default = client.get(_URL)
        explicit = client.get(f"{_URL}?limit=2")

    assert _messages(default.json()) == ["row 0", "row 1", "row 2"]
    assert _messages(explicit.json()) == ["row 0", "row 1"]


def test_a_loki_that_cannot_answer_is_a_retriable_503() -> None:
    with _loki() as (stub, client):
        stub.script.status = 500
        response = client.get(_URL)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


@pytest.mark.parametrize(
    "query",
    ["agent_id=1&service_only=true", "level=NOPE", "since=garbage", "limit=1001", "limit=0"],
)
def test_a_filter_that_cannot_be_satisfied_is_a_400(query: str) -> None:
    with _loki() as (stub, client):
        response = client.get(f"{_URL}?{query}")

    assert response.status_code == 400
    assert stub.script.queries == []
