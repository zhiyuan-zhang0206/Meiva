# meiva:note
# GET /api/cluster/admin/events over real `telemetry_events` rows.
#
# The rules about the filters (limit range, level threshold, `since` forms, the default
# window) are asserted on the planned query in `gateway/cluster/tests/test_admin_events_plan.py`;
# these tests show the endpoint returns the rows that query selects.

from __future__ import annotations

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.cluster_endpoints.cluster_support import seed_event

_URL = "/api/cluster/admin/events"


def _messages(body: dict[str, object]) -> list[str]:
    # meiva:note
    # The annotated shape is the one the untyped call returns.
    items: list[dict[str, dict[str, str]]] = body["items"]  # pyright: ignore[reportAssignmentType]
    return [item["payload"]["msg"] for item in items]


def test_returns_newest_first_in_the_row_shape(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, msg="oldest", seconds_ago=300)
    seed_event(db_conn, msg="newest", seconds_ago=10, agent_id=1)

    with gateway_test_client() as client:
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


def test_agent_id_filter_keeps_only_that_agents_events(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, msg="mine", agent_id=3)
    seed_event(db_conn, msg="other agent", agent_id=4)
    seed_event(db_conn, msg="service")

    with gateway_test_client() as client:
        response = client.get(f"{_URL}?agent_id=3")

    assert _messages(response.json()) == ["mine"]


def test_service_only_keeps_events_with_no_agent(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, msg="service")
    seed_event(db_conn, msg="agent", agent_id=3)

    with gateway_test_client() as client:
        response = client.get(f"{_URL}?service_only=true")

    assert _messages(response.json()) == ["service"]


def test_level_is_a_minimum(db_conn: psycopg.Connection) -> None:
    for level in ("debug", "info", "warning", "error", "critical"):
        seed_event(db_conn, msg=level, level=level)

    with gateway_test_client() as client:
        response = client.get(f"{_URL}?level=WARNING")

    assert sorted(_messages(response.json())) == ["critical", "error", "warning"]


def test_since_bounds_the_window_in_both_forms(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, msg="recent", seconds_ago=30)
    seed_event(db_conn, msg="two hours old", seconds_ago=7200)

    with gateway_test_client() as client:
        relative = client.get(f"{_URL}?since=1h")
        absolute = client.get(f"{_URL}?since=2020-01-01T00:00:00Z")

    assert _messages(relative.json()) == ["recent"]
    assert _messages(absolute.json()) == ["recent", "two hours old"]


def test_the_default_window_is_the_last_day(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, msg="inside", seconds_ago=23 * 3600)
    seed_event(db_conn, msg="outside", seconds_ago=25 * 3600)

    with gateway_test_client() as client:
        response = client.get(_URL)

    assert _messages(response.json()) == ["inside"]


def test_event_names_and_grep_narrow_the_rows(db_conn: psycopg.Connection) -> None:
    seed_event(db_conn, event_name="spawn", msg="hello world")
    seed_event(db_conn, event_name="terminate", msg="bye")
    seed_event(db_conn, event_name="log", msg="hello again")

    with gateway_test_client() as client:
        by_name = client.get(f"{_URL}?event=spawn,terminate")
        by_text = client.get(f"{_URL}?grep=hello")

    assert sorted(_messages(by_name.json())) == ["bye", "hello world"]
    assert sorted(_messages(by_text.json())) == ["hello again", "hello world"]


def test_an_explicit_limit_wins_and_the_default_comes_from_display_config(
    db_conn: psycopg.Connection,
) -> None:
    for index in range(4):
        seed_event(db_conn, msg=f"row {index}", seconds_ago=10 + index)

    with gateway_test_client(fields={"cluster_events_default_limit": 3}) as client:
        default = client.get(_URL)
        explicit = client.get(f"{_URL}?limit=2")

    assert _messages(default.json()) == ["row 0", "row 1", "row 2"]
    assert _messages(explicit.json()) == ["row 0", "row 1"]


@pytest.mark.parametrize(
    "query",
    ["agent_id=1&service_only=true", "level=NOPE", "since=garbage", "limit=1001", "limit=0"],
)
def test_a_filter_that_cannot_be_satisfied_is_a_400(query: str) -> None:
    with gateway_test_client() as client:
        response = client.get(f"{_URL}?{query}")

    assert response.status_code == 400
