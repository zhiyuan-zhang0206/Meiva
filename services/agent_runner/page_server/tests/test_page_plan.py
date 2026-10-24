# meiva:note
# What one page-server pass decides, over values.

from __future__ import annotations

from dataclasses import replace

import pytest

from services.agent_runner.page_server.page_plan import (
    PAGE_SERVER_MODULE,
    SPAWN_VERIFY_TIMEOUT_S,
    Admission,
    PageRow,
    ServerHandle,
    admission,
    health_due,
    server_command,
    sessions_to_close,
    shell_pids,
    stale_handles,
)

_SESSION = "meiva-agent-7-shell-0-page-docs"
_ROW = PageRow(
    id=1,
    agent_id=7,
    name="docs",
    port=12001,
    host="127.0.0.1",
    serve_dir="/site",
    session_name=_SESSION,
)
_HANDLE = ServerHandle(7, "docs", 12001, "/site", "token", _SESSION, 100.0)


def test_the_command_that_starts_a_row_names_its_port_host_and_directory() -> None:
    assert server_command("/venv/bin/python", 12001, "10.0.0.5", "/srv/my site") == (
        f"/venv/bin/python -m {PAGE_SERVER_MODULE} --port 12001 --host 10.0.0.5 --dir '/srv/my site'"
    )


def test_a_row_and_a_handle_that_agree_are_not_stale() -> None:
    assert stale_handles({(7, "docs"): _ROW}, {(7, "docs"): _HANDLE}) == ()


@pytest.mark.parametrize(
    "changed",
    [
        replace(_ROW, port=12002),
        replace(_ROW, serve_dir="/other"),
        replace(_ROW, session_name=None),
        replace(_ROW, session_name="renamed"),
    ],
)
def test_a_handle_whose_row_changed_is_stale(changed: PageRow) -> None:
    assert stale_handles({(7, "docs"): changed}, {(7, "docs"): _HANDLE}) == ((7, "docs"),)


def test_a_handle_whose_row_is_gone_is_stale() -> None:
    managed = {(7, "docs"): _HANDLE, (7, "kept"): replace(_HANDLE, name="kept")}
    wanted = {(7, "kept"): replace(_ROW, name="kept")}
    assert stale_handles(wanted, managed) == ((7, "docs"),)


def test_only_the_persisted_sessions_that_are_alive_are_closed_in_listing_order() -> None:
    assert sessions_to_close(["b", "a", "gone", "c"], {"a", "b", "c", "other"}) == ("b", "a", "c")


@pytest.mark.parametrize(
    ("session", "live", "directory_exists", "expected"),
    [
        (None, set[str](), True, Admission.CREATE),
        ("page-a", {"page-a"}, True, Admission.ADOPT),
        ("page-a", set[str](), True, Admission.SESSION_LOST),
        (None, set[str](), False, Admission.DIRECTORY_MISSING),
        ("page-a", {"page-a"}, False, Admission.DIRECTORY_MISSING),
    ],
)
def test_admission_decides_adopt_create_or_close(
    session: str | None, live: set[str], directory_exists: bool, expected: Admission
) -> None:
    row = replace(_ROW, session_name=session)
    assert admission(row, live, directory_exists=directory_exists) is expected


def test_a_server_is_probed_only_once_its_spawn_window_is_over() -> None:
    handle = replace(_HANDLE, last_launch_monotonic=100.0)
    assert not health_due(handle, 100.0 + SPAWN_VERIFY_TIMEOUT_S - 0.1)
    assert health_due(handle, 100.0 + SPAWN_VERIFY_TIMEOUT_S)


def test_shell_pids_map_live_sessions_to_the_pages_that_name_them() -> None:
    wanted = {
        (7, "docs"): replace(_ROW, session_name="page-docs"),
        (7, "lost"): replace(_ROW, name="lost", session_name="page-lost"),
        (7, "fresh"): replace(_ROW, name="fresh", session_name=None),
    }
    live = {"page-docs": 4001, "unrelated": 4002}
    assert shell_pids(wanted, live) == {4001: (7, "docs")}
