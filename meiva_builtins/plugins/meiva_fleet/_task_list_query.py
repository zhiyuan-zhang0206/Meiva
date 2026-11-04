from __future__ import annotations

from psycopg import sql

from base.agents.tasks.model import TASK_COLUMNS


# meiva:note
# The task columns as a SQL fragment of identifiers, so a statement built from them stays a
# composed query instead of a formatted string.
def task_columns() -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(column) for column in TASK_COLUMNS.split(", "))


# meiva:note
# SQL + params for list(): a plain filter query, or a recursive CTE over
# the whole subtree rooted at `parent`. parent_id always points at an older
# row (a child is created after its parent), so the tree cannot cycle and the
# recursion terminates. owner / status filter the subtree at the end.
def build_list_query(
    parent: int | None,
    owner: int | None,
    status: str | None,
    *,
    recursive: bool,
) -> tuple[sql.Composed, list[object]]:
    filters: list[sql.Composable] = []
    params: list[object] = []
    if owner is not None:
        filters.append(sql.SQL("owner = %s"))
        params.append(owner)
    if status is not None:
        filters.append(sql.SQL("status = %s"))
        params.append(status)

    if recursive and parent is not None:
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(filters) if filters else sql.SQL("")
        query = sql.SQL(
            "WITH RECURSIVE subtree AS ("
            " SELECT * FROM agent_tasks WHERE parent_id = %s"
            " UNION ALL"
            " SELECT c.* FROM agent_tasks c JOIN subtree s ON c.parent_id = s.id"
            ") SELECT {columns} FROM subtree{where} ORDER BY created_at, id"
        ).format(columns=task_columns(), where=where)
        return query, [parent, *params]

    if parent is not None:
        filters.append(sql.SQL("parent_id = %s"))
        params.append(parent)
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(filters) if filters else sql.SQL("")
    query = sql.SQL("SELECT {columns} FROM agent_tasks{where} ORDER BY created_at, id").format(
        columns=task_columns(), where=where
    )
    return query, params
