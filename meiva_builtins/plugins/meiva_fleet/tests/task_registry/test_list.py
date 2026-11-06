# meiva:note
# `task_registry.list` filters: owner, status, direct children and the whole subtree.

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing

import psycopg
import pytest

from base.agents.context.clients import ClientSet
from base.db import Database
from meiva.tests.fixtures.pin_agent import pin_agent, snapshot_clients_over
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import seed_agent


def _create(title: str, parent: int) -> int:
    return task_registry.create(
        title,
        "work",
        parent=parent,
    ).id


@pytest.fixture
def task_list_clients(database: Database) -> Iterator[ClientSet]:
    with closing(snapshot_clients_over(lambda: database)) as clients:
        yield clients


def test_list_filters_by_owner_status_parent_and_subtree(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, task_list_clients: ClientSet
) -> None:
    owner = seed_agent(db_conn)
    pin_agent(owner, clients=task_list_clients)
    top = _create("top", root_task_id)
    child = _create("child", top)
    grandchild = _create("grandchild", child)
    other = _create("other", root_task_id)
    task_registry.update(
        other,
        status="done",
        results="finished",
    )

    def ids(
        *,
        parent: int | None = None,
        owner: int | None = None,
        status: str | None = None,
        recursive: bool = False,
    ) -> list[int]:
        found = task_registry.list(parent=parent, owner=owner, status=status, recursive=recursive)
        return [task.id for task in found]

    everything = [root_task_id, top, child, grandchild, other]
    assert ids() == everything
    assert ids(owner=owner) == [top, child, grandchild, other]
    assert ids(status="done") == [other]
    assert ids(status="in_progress") == [root_task_id, top, child, grandchild]
    assert ids(recursive=True) == everything
    assert ids(parent=top) == [child]
    assert ids(parent=top, recursive=True) == [child, grandchild]
    assert ids(parent=top, recursive=True, status="in_progress") == [child, grandchild]
    assert ids(owner=owner, status="done") == [other]
    assert ids(parent=root_task_id, status="done") == [other]
    assert ids(parent=root_task_id, recursive=True, owner=owner, status="done") == [other]
    assert ids(parent=root_task_id, recursive=True) == [top, child, grandchild, other]
