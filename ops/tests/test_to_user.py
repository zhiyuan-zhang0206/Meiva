# meiva:note
# The to-user read door against the throwaway Postgres and a real unit home: the agent's machine
# is the `agents_meta` row, the workspace is a directory under the home, and nothing is patched.

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.agents.to_user import DOCUMENT_FILENAME, to_user_limits
from base.config import settings
from base.db import Database, create_agent
from ops.to_user import (
    FileReach,
    WorkspaceFileError,
    owned_workspace,
    read_at_most,
    read_to_user,
    read_workspace_file_content,
)

# meiva:note
# The read caps the door takes as a value, at the numbers the process is configured with.
LIMITS = to_user_limits(settings.to_user)


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
    db_conn: psycopg.Connection, database: Database, unit_home: Path, tmp_path: Path
) -> None:
    agent_id = _agent_on(db_conn, "source-host")
    served_home = tmp_path / "served-home"

    with database.pool() as pool:
        workspace = owned_workspace(pool, agent_id, "source-host", home=served_home)

    assert workspace == served_home / "workspaces" / str(agent_id)
    assert unit_home != served_home
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
            owned_workspace(pool, agent_id, "wrong-host", home=unit_home)
        with pytest.raises(ValueError, match="home machine"):
            owned_workspace(pool, agent_id + 999, "source-host", home=unit_home)


# meiva:note
# The document is read from the agent's workspace on its home machine.
def test_the_document_is_read_from_the_home_machines_workspace(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    agent_id = _agent_on(db_conn, "source-host")
    served_home = tmp_path / "served-home"
    workspace = served_home / "workspaces" / str(agent_id)
    workspace.mkdir(parents=True)
    (workspace / DOCUMENT_FILENAME).write_text("# Report\n\nDone.\n", encoding="utf-8")

    with database.pool() as pool:
        document = read_to_user(
            pool, agent_id, "source-host", files=[], home=served_home, limits=LIMITS
        )

    assert document.content == "# Report\n\nDone.\n"
    assert document.version is not None


def _workspaces_reach(served_home: Path, tmp_path: Path) -> FileReach:
    return FileReach(home=served_home, user_home=tmp_path / "account", access="workspaces")


def test_out_of_bounds_absolute_paths_raise_403_instead_of_404(tmp_path: Path) -> None:
    served_home = tmp_path / "served-home"
    workspace = served_home / "workspaces" / "10"
    workspace.mkdir(parents=True)
    _ = (workspace / "inside.txt").write_text("allowed", encoding="utf-8")
    reach = _workspaces_reach(served_home, tmp_path)

    for forbidden in (
        "/etc/passwd",
        "/Users/other/secret.txt",
        "/Users/other/.meiva/workspaces/20/a.png",
    ):
        with pytest.raises(WorkspaceFileError) as exc_info:
            _ = read_workspace_file_content(reach, 10, forbidden)
        assert exc_info.value.status_code == 403


def test_explicit_same_owner_workspace_path_resolves_across_host_prefixes_and_blocks_traversal(
    tmp_path: Path,
) -> None:
    served_home = tmp_path / "served-home"
    workspace = served_home / "workspaces" / "10"
    deliver = workspace / "deliver"
    deliver.mkdir(parents=True)
    _ = (deliver / "report.md").write_text("from workspace", encoding="utf-8")

    custom_cwd = tmp_path / "custom-cwd"
    (custom_cwd / "deliver").mkdir(parents=True)
    _ = (custom_cwd / "deliver" / "report.md").write_text("from cwd", encoding="utf-8")

    reach = _workspaces_reach(served_home, tmp_path)
    result = read_workspace_file_content(
        reach,
        10,
        "file:///Users/zhiyuan-output/.meiva/workspaces/10/deliver/report.md#L1",
        custom_cwd=str(custom_cwd),
    )
    assert result.content == "from workspace"
    assert result.path == str((deliver / "report.md").resolve())
    assert result.root.model_dump() == {
        "kind": "workspace",
        "path": str(workspace.resolve()),
        "agent_id": 10,
    }

    with pytest.raises(WorkspaceFileError) as exc_info:
        _ = read_workspace_file_content(
            reach,
            10,
            "/Users/zhiyuan-output/.meiva/workspaces/10/deliver/../../20/secret.md",
        )
    assert exc_info.value.status_code == 403


# meiva:note
# The shared reader takes no reported size at all: a file that reports 0 (procfs) is still cut
# at the byte limit. A real file exercises both one byte over the limit and the exact boundary.
def test_reads_stop_at_the_limit_whatever_the_reported_size(tmp_path: Path) -> None:
    stream = tmp_path / "stream.txt"
    payload = b"x" * 17
    _ = stream.write_bytes(payload)

    with pytest.raises(WorkspaceFileError) as error:
        _ = read_at_most(stream, 16, "File exceeds the size limit")
    assert error.value.status_code == 413
    assert read_at_most(stream, 17, "File exceeds the size limit") == payload
