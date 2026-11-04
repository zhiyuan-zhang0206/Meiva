# meiva:note
# `task_registry.list` filters: owner, status, direct children and the whole subtree.

from __future__ import annotations

from uuid import uuid4

import psycopg

from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import seed_agent


def _create(title: str, parent: int) -> int:
    return task_registry.create(title, "work", parent=parent, operation_key=str(uuid4())).id


def test_list_filters_by_owner_status_parent_and_subtree(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    owner = seed_agent(db_conn)
    pin_agent(owner)
    top = _create("top", root_task_id)
    child = _create("child", top)
    grandchild = _create("grandchild", child)
    other = _create("other", root_task_id)
    task_registry.update(
        other,
        status="done",
        results="finished",
        operation_key=str(uuid4()),
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

    assert ids(parent=top) == [child]
    assert ids(parent=top, recursive=True) == [child, grandchild]
    assert ids(parent=top, recursive=True, status="in_progress") == [child, grandchild]
    assert ids(owner=owner, status="done") == [other]
    assert ids(parent=root_task_id, status="done") == [other]
    assert ids(parent=root_task_id, recursive=True, owner=owner, status="done") == [other]
    assert ids(parent=root_task_id, recursive=True) == [top, child, grandchild, other]
