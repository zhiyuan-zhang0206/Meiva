# meiva:note
# One agent's shell sessions over a real pty-sessions service and the throwaway database.
from __future__ import annotations

import time
from pathlib import Path

import pytest

from base.db import Database
from base.host.runtime_interpreter import runtime_venv
from base.paths import workspace_dir
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions


def _create(
    shell: ShellSessions, host: SessionHost, name: str | None, *, cwd: Path, ttl: float = 120.0
) -> int:
    session_id, _full = shell.create(name, cwd=cwd, ttl=ttl, host=host)
    return session_id


def _screen_has(shell: ShellSessions, session_id: int, needle: str, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if needle in "".join(shell.capture(session_id, 200, scrollback=True).split()):
            return True
        time.sleep(0.1)
    return False


def test_a_created_session_carries_its_owners_identity(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    # meiva:note
    # Every session names its owner: a script run in it constructs the owner's complete MeivaContext from the environment (ruling 2026-10-04, source 2).
    session_id = _create(live_shell, live_host, "probe", cwd=tmp_path)

    live_shell.send(session_id, "echo owner-is-$MEIVA_AGENT_ID")

    assert _screen_has(live_shell, session_id, "owner-is-41")


def test_a_session_starts_where_it_is_told_and_is_listed_by_name(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    unnamed = _create(live_shell, live_host, None, cwd=workspace_dir(live_shell.agent_id))
    named = _create(live_shell, live_host, "dev-server", cwd=tmp_path)

    assert (unnamed, named) == (0, 1)
    assert live_shell.list() == {0: None, 1: "dev-server"}
    live_shell.send(named, "pwd")
    assert _screen_has(live_shell, named, tmp_path.name)


def test_bare_python_resolves_into_the_venv_whatever_the_profile_does(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, tmp_path: Path
) -> None:
    # meiva:note
    # A persistent session is a login+interactive shell (`bash -l -i`, `base/sessions/pty/launch.py::_fork_shell`), so it sources the user's shell profile, which could in principle put something ahead of the venv bin dir `forward_env_dict` forwards on PATH. QA for #3590 verified by hand that bare `python` still resolves into this checkout's venv inside such a session. The test home holds a minimal `.bash_profile`, so the assertion depends only on the mechanism this repo ships and not on the developer machine's own shell customization (pyenv/conda/nvm rewriting PATH).
    (unit_home / ".bash_profile").write_text("# minimal profile — no PATH edits\n")
    session_id = _create(live_shell, live_host, "venv-python", cwd=tmp_path)

    live_shell.send(session_id, "command -v python")

    assert _screen_has(live_shell, session_id, str(runtime_venv() / "bin" / "python"))


def test_a_refused_name_or_ttl_takes_no_session_id(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="lowercase slug"):
        _create(live_shell, live_host, "Dev Server", cwd=tmp_path)
    with pytest.raises(ValueError, match="reserved for pages"):
        _create(live_shell, live_host, "page-preview", cwd=tmp_path)
    with pytest.raises(ValueError, match="ttl"):
        _create(live_shell, live_host, "late", cwd=tmp_path, ttl=86400.1)

    assert _create(live_shell, live_host, "first", cwd=tmp_path) == 0


def test_a_system_session_may_outlive_the_user_cap(
    live_shell: ShellSessions, live_host: SessionHost, database: Database, tmp_path: Path
) -> None:
    session_id, _full = live_shell.create(
        "watcher", cwd=tmp_path, ttl=7 * 86400.0, system=True, host=live_host
    )

    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at > now() + interval '6 days' FROM agent_shell_ttls "
            "WHERE agent_id = %s AND session_id = %s",
            (live_shell.agent_id, session_id),
        )
        row = cur.fetchone()
    assert row == (True,)


def test_a_session_without_a_deadline_row_is_disposed_of(
    live_shell: ShellSessions, live_host: SessionHost, database: Database, tmp_path: Path
) -> None:
    # meiva:note
    # The reaper reads `agent_shell_ttls`, so a session whose row cannot be written could never be reclaimed. The row for id 0 is taken beforehand, so the insert of this creation conflicts.
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 0, now() + interval '1 hour')",
            (live_shell.agent_id,),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="failed to track TTL for session 0"):
        _create(live_shell, live_host, "orphan", cwd=tmp_path)

    assert live_shell.list() == {}


def test_a_session_that_cannot_start_is_refused(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="failed to create session"):
        _create(live_shell, live_host, "broken", cwd=tmp_path / "does-not-exist")

    assert live_shell.list() == {}


def test_kill_removes_the_session_and_its_id_stays_stale(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    session_id = _create(live_shell, live_host, "doomed", cwd=tmp_path)

    live_shell.kill(session_id)

    assert live_shell.list() == {}
    with pytest.raises(ValueError, match="not this agent's"):
        live_shell.send(session_id, "echo nope")
    assert _create(live_shell, live_host, "next", cwd=tmp_path) == session_id + 1


def test_kill_all_removes_every_session_of_the_agent(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    _create(live_shell, live_host, "one", cwd=tmp_path)
    _create(live_shell, live_host, "two", cwd=tmp_path)

    assert live_shell.kill_all() == 2
    assert live_shell.list() == {}
