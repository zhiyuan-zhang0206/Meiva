# meiva:note
# The to-user read door against the throwaway Postgres and a real unit home: the agent's machine
# is the `agents_meta` row, the workspace is a directory under the home, and nothing is patched.

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.agents.to_user import (
    DOCUMENT_FILENAME,
    ToUserLimits,
    WorkspaceFileRequest,
    to_user_limits,
)
from base.config import settings
from base.daemon.http_transport import FileBody
from base.db import Database, create_agent
from ops.to_user import (
    FileReach,
    WorkspaceFileError,
    answer_workspace_file,
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


OPEN_RANGE_BYTES = 1000
RANGE_RESOURCE_BYTES = 5000

RANGE_LIMITS = ToUserLimits(
    document_bytes=1024,
    references=4,
    chain_depth=2,
    resource_bytes=RANGE_RESOURCE_BYTES,
    open_range_bytes=OPEN_RANGE_BYTES,
)
CLIP = bytes(i % 251 for i in range(3000))


@pytest.fixture
def clip_file(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp3"
    path.write_bytes(CLIP)
    return path


def _range_answer(
    path: Path, byte_range: str | None, tmp_path: Path
) -> tuple[int, bytes, str, dict[str, str]]:
    request = WorkspaceFileRequest(
        agent_id=1, path=str(path), mode="raw", byte_range=byte_range, file_access="all"
    )
    answer = answer_workspace_file(
        request, home=tmp_path / "home", user_home=tmp_path, limits=RANGE_LIMITS
    )
    status, body, content_type = answer[:3]
    headers = dict(answer[3]) if len(answer) == 4 else {}
    if isinstance(body, FileBody):
        with body.handle:
            body = body.handle.read(body.length)
    return status, body, content_type, headers


def test_an_open_ended_range_is_answered_in_slices_that_chain(
    clip_file: Path, tmp_path: Path
) -> None:
    received = b""
    offset = 0
    ranges: list[str] = []
    while offset < len(CLIP):
        status, body, _, headers = _range_answer(clip_file, f"bytes={offset}-", tmp_path)
        assert status == 206
        assert len(body) == min(OPEN_RANGE_BYTES, len(CLIP) - offset)
        ranges.append(headers["Content-Range"])
        received += body
        offset += len(body)
    assert received == CLIP
    assert ranges == [
        "bytes 0-999/3000",
        "bytes 1000-1999/3000",
        "bytes 2000-2999/3000",
    ]


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-1999", 0, 1999),
        ("bytes=500-2500", 500, 2500),
        ("bytes=2990-999999", 2990, 2999),
        ("bytes=0-0", 0, 0),
        ("bytes=-1500", 1500, 2999),
        ("bytes=-999999", 0, 2999),
    ],
)
def test_a_range_that_names_its_end_is_honored_in_full(
    clip_file: Path, tmp_path: Path, header: str, first: int, last: int
) -> None:
    status, body, _, headers = _range_answer(clip_file, header, tmp_path)
    assert status == 206
    assert body == CLIP[first : last + 1]
    assert headers["Content-Range"] == f"bytes {first}-{last}/3000"


def test_the_tail_and_the_resource_limit_bound_a_slice(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (RANGE_RESOURCE_BYTES * 2))
    _, tail, _, tail_headers = _range_answer(
        big, f"bytes={RANGE_RESOURCE_BYTES * 2 - 10}-", tmp_path
    )
    assert (len(tail), tail_headers["Content-Range"]) == (
        10,
        f"bytes 9990-9999/{RANGE_RESOURCE_BYTES * 2}",
    )
    _, capped, _, headers = _range_answer(big, f"bytes=0-{RANGE_RESOURCE_BYTES * 2 - 1}", tmp_path)
    assert len(capped) == RANGE_RESOURCE_BYTES
    assert (
        headers["Content-Range"] == f"bytes 0-{RANGE_RESOURCE_BYTES - 1}/{RANGE_RESOURCE_BYTES * 2}"
    )


@pytest.mark.parametrize("header", ["bytes=3000-", "bytes=9999-10000", "bytes=-0"])
def test_a_range_that_starts_past_the_end_is_not_satisfiable(
    clip_file: Path, tmp_path: Path, header: str
) -> None:
    status, body, _, headers = _range_answer(clip_file, header, tmp_path)
    assert (status, body, headers["Content-Range"]) == (416, b"", "bytes */3000")


@pytest.mark.parametrize(
    "header", ["items=0-5", "bytes=0-5,10-20", "bytes=abc-", "bytes=9-3", "bytes=", "garbage"]
)
def test_a_range_the_server_may_ignore_answers_the_whole_file(
    clip_file: Path, tmp_path: Path, header: str
) -> None:
    status, body, _, headers = _range_answer(clip_file, header, tmp_path)
    assert (status, body, headers) == (200, CLIP, {})


def test_a_read_without_a_range_streams_the_whole_file(clip_file: Path, tmp_path: Path) -> None:
    request = WorkspaceFileRequest(agent_id=1, path=str(clip_file), mode="raw", file_access="all")
    answer = answer_workspace_file(
        request, home=tmp_path / "home", user_home=tmp_path, limits=RANGE_LIMITS
    )
    assert answer[0] == 200
    assert isinstance(answer[1], FileBody)
    assert answer[1].length == len(CLIP)
    answer[1].handle.close()


def test_a_file_over_the_resource_limit_is_refused_before_streaming(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (RANGE_RESOURCE_BYTES + 1))
    with pytest.raises(WorkspaceFileError) as refused:
        _ = _range_answer(big, None, tmp_path)
    assert refused.value.status_code == 413


def test_a_file_that_reports_no_size_is_read_in_one_piece(tmp_path: Path) -> None:
    if not Path("/proc/self/stat").exists():
        return
    status, body, _, _ = _range_answer(Path("/proc/self/stat"), None, tmp_path)
    assert (status, len(body) > 0) == (200, True)


def test_an_empty_file_answers_empty_and_no_range_of_it_is_satisfiable(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert _range_answer(empty, None, tmp_path)[:2] == (200, b"")
    status, _, _, headers = _range_answer(empty, "bytes=0-", tmp_path)
    assert (status, headers["Content-Range"]) == (416, "bytes */0")
