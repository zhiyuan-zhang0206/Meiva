# meiva:note
# GET /api/tasks `owner` and `status` — the optional filters `meiva tasks ls` forwards, against the
# real task table: each narrows the list in SQL, they combine with AND and with `fields`, a value
# outside the vocabulary is a 422, and no filter is the unchanged full registry.

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.test_tasks_router import make_agent, make_task


def _ids(**params: str | int) -> set[int]:
    with gateway_test_client() as client:
        response = client.get("/api/tasks", params=params)
    assert response.status_code == 200, response.text
    return {task["id"] for task in response.json()["tasks"]}


def test_owner_narrows_the_list_to_that_agents_tasks(db_conn: psycopg.Connection) -> None:
    mine, other = make_agent(db_conn), make_agent(db_conn)
    wanted = make_task(db_conn, owner=mine, title="a")
    make_task(db_conn, owner=other, title="b")
    assert _ids(owner=mine) == {wanted}


def test_status_narrows_the_list_to_that_status(db_conn: psycopg.Connection) -> None:
    owner = make_agent(db_conn)
    open_task = make_task(db_conn, owner=owner, title="open")
    done_task = make_task(db_conn, owner=owner, title="finished", status="done")
    assert _ids(status="done", owner=owner) == {done_task}
    assert _ids(status="in_progress", owner=owner) == {open_task}


def test_owner_and_status_combine_with_and(db_conn: psycopg.Connection) -> None:
    mine, other = make_agent(db_conn), make_agent(db_conn)
    make_task(db_conn, owner=mine, title="mine open")
    wanted = make_task(db_conn, owner=mine, title="mine done", status="done")
    make_task(db_conn, owner=other, title="theirs done", status="done")
    assert _ids(owner=mine, status="done") == {wanted}


def test_the_filters_apply_to_every_projection(db_conn: psycopg.Connection) -> None:
    mine, other = make_agent(db_conn), make_agent(db_conn)
    wanted = make_task(db_conn, owner=mine, title="a")
    make_task(db_conn, owner=other, title="b")
    for fields in ("full", "graph", "summary"):
        assert _ids(owner=mine, fields=fields) == {wanted}


def test_no_filter_is_the_whole_registry(db_conn: psycopg.Connection) -> None:
    mine, other = make_agent(db_conn), make_agent(db_conn)
    first = make_task(db_conn, owner=mine, title="a")
    second = make_task(db_conn, owner=other, title="b", status="done")
    assert {first, second} <= _ids()


@pytest.mark.parametrize("params", [{"status": "ongoing"}, {"owner": 0}, {"owner": "x"}])
def test_a_value_outside_the_vocabulary_is_422(params: dict[str, Any]) -> None:
    with gateway_test_client() as client:
        assert client.get("/api/tasks", params=params).status_code == 422
