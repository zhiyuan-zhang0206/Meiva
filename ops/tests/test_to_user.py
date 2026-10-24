# meiva:note
# The to-user read door against the throwaway Postgres and a real unit home: the agent's machine
# is the `agents_meta` row, the workspace is a directory under the home, and nothing is patched.

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.agents.to_user import DOCUMENT_FILENAME
from base.db import Database, create_agent
from ops.to_user import owned_workspace, read_to_user


def _agent_on(db_conn: psycopg.Connection, machine: str) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
        (agent_id, machine),
    )
    db_conn.commit()
    return agent_id


# meiva:note
# On the agent's home machine the workspace is the agent's directory under the unit home, and
# resolving it creates nothing: a reader must not manufacture a workspace that does not exist.
def test_the_home_machine_resolves_the_agents_workspace_without_creating_it(
    db_conn: psycopg.Connection, database: Database, unit_home: Path
) -> None:
    agent_id = _agent_on(db_conn, "source-host")

    with database.pool() as pool:
        workspace = owned_workspace(pool, agent_id, "source-host")

    assert workspace == unit_home / "workspaces" / str(agent_id)
    assert not workspace.exists()


# meiva:note
# Another machine, and an agent nobody registered, are refused with the same message: the
# document lives on the agent's home machine only.
def test_another_machine_or_an_unknown_agent_is_refused(
    db_conn: psycopg.Connection, database: Database, unit_home: Path
) -> None:
    agent_id = _agent_on(db_conn, "source-host")

    with database.pool() as pool:
        with pytest.raises(ValueError, match="home machine"):
            owned_workspace(pool, agent_id, "wrong-host")
        with pytest.raises(ValueError, match="home machine"):
            owned_workspace(pool, agent_id + 999, "source-host")


# meiva:note
# The document is read from the agent's workspace on its home machine.
def test_the_document_is_read_from_the_home_machines_workspace(
    db_conn: psycopg.Connection, database: Database, unit_home: Path
) -> None:
    agent_id = _agent_on(db_conn, "source-host")
    workspace = unit_home / "workspaces" / str(agent_id)
    workspace.mkdir(parents=True)
    (workspace / DOCUMENT_FILENAME).write_text("# Report\n\nDone.\n", encoding="utf-8")

    with database.pool() as pool:
        document = read_to_user(pool, agent_id, "source-host", references=[])

    assert document.content == "# Report\n\nDone.\n"
    assert document.version is not None
