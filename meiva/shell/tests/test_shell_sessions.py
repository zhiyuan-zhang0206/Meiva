# meiva:note
# One agent's shell sessions over a real pty-sessions service and the throwaway database.
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import psycopg
import pytest

from base.db import Database
from base.host.runtime_interpreter import runtime_venv
from base.paths import meiva_home, workspace_dir
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import DelayedNewProxy, service_socket
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


def _captured_thread(operation: Callable[[], None]) -> tuple[Thread, list[BaseException]]:
    failures: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=run)
    thread.start()
    return thread, failures


def _terminal_transition(
    agent_id: int,
    database: Database,
    started: Event,
    finished: Event,
) -> int:
    started.set()
    try:
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_index FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,)
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' "
                "WHERE id = %s",
                (agent_id,),
            )
            conn.commit()
            return int(row[0])
    finally:
        finished.set()


def _terminal_transition_after_fenced_creation(
    shell: ShellSessions,
    database: Database,
    tmp_path: Path,
    unit_home: Path,
) -> int:
    from meiva.shell import session_plan, ttl_store

    creation_entered = Event()
    release_creation = Event()
    created: list[int] = []
    terminal_cutoffs: list[int] = []
    terminal_failures: list[BaseException] = []
    terminal_started = Event()
    terminal_finished = Event()

    def start(session_id: int) -> str:
        full = session_plan.session_full_name(shell.shell_prefix(), session_id, "fenced")
        creation_entered.set()
        if not release_creation.wait(timeout=5.0):
            raise TimeoutError("test did not release the fenced session creation")
        assert shell.backend.new_session(full, "", tmp_path, env={"MEIVA_HOME": str(unit_home)})
        return full

    def dispose(full: str) -> None:
        shell.backend.kill_session(full, graceful=False)

    creator, failures = _captured_thread(
        lambda: created.append(
            ttl_store.create_ttl_fenced(
                database, shell.agent_id, 120.0, start=start, dispose=dispose
            )[0]
        )
    )
    terminal: Thread | None = None
    try:
        assert creation_entered.wait(timeout=5.0)
        terminal, terminal_failures = _captured_thread(
            lambda: terminal_cutoffs.append(
                _terminal_transition(shell.agent_id, database, terminal_started, terminal_finished)
            )
        )
        assert terminal_started.wait(timeout=5.0)
        with database.connect() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                cur.execute(
                    "SELECT 1 FROM agents_meta WHERE id = %s FOR UPDATE NOWAIT",
                    (shell.agent_id,),
                )
            conn.rollback()
        assert not terminal_finished.wait(timeout=0.1)
    finally:
        release_creation.set()
        creator.join(timeout=5.0)
        if terminal is not None:
            terminal.join(timeout=5.0)

    assert not creator.is_alive()
    assert terminal is not None
    assert not terminal.is_alive()
    assert failures == []
    assert terminal_failures == []
    assert created == [0]
    assert terminal_cutoffs == [1]
    return terminal_cutoffs[0]


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
    unnamed = _create(
        live_shell, live_host, None, cwd=workspace_dir(live_shell.agent_id, home=meiva_home())
    )
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
    with pytest.raises(ValueError, match="ttl"):
        _create(live_shell, live_host, "late", cwd=tmp_path, ttl=86400.1)

    assert _create(live_shell, live_host, "first", cwd=tmp_path) == 0


def test_a_system_session_may_outlive_the_user_cap_but_not_the_absolute_cap(
    live_shell: ShellSessions, live_host: SessionHost, database: Database, tmp_path: Path
) -> None:
    session_id, _full = live_shell.create(
        "watcher", cwd=tmp_path, ttl=72 * 3600.0, system=True, host=live_host
    )

    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at > now() + interval '71 hours' FROM agent_shell_ttls "
            "WHERE agent_id = %s AND session_id = %s",
            (live_shell.agent_id, session_id),
        )
        row = cur.fetchone()
    assert row == (True,)

    with pytest.raises(ValueError, match="at most 259200 seconds"):
        live_shell.create(
            "too-long", cwd=tmp_path, ttl=72 * 3600.0 + 1, system=True, host=live_host
        )

    # meiva:note
    # ShellSessions is usable directly by internal callers, so its renewal
    # boundary must enforce the cap independently of the public SDK wrapper.
    with pytest.raises(ValueError, match="at most 86400 seconds"):
        live_shell.renew(session_id, 24 * 3600.0 + 1)


def test_a_terminated_agent_cannot_create_a_late_shell(
    live_shell: ShellSessions, live_host: SessionHost, database: Database, tmp_path: Path
) -> None:
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (live_shell.agent_id,)
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="missing or terminated"):
        live_shell.create("late", cwd=tmp_path, ttl=120.0, host=live_host)

    assert live_shell.list() == {}


# meiva:note
# The creator holds the agents_meta row lock after taking its id but before the
# PTY service answers. A terminal transition uses that exact row lock to capture
# its cutoff, so NOWAIT must lose here. Once the creation commits, termination captures
# it below the cutoff; even a resurrection before post-commit cleanup cannot keep
# that pre-terminal session or its TTL row alive.
def test_terminal_transition_cannot_pass_a_half_created_session(
    live_shell: ShellSessions,
    live_host: SessionHost,
    database: Database,
    tmp_path: Path,
    unit_home: Path,
) -> None:
    cutoff = _terminal_transition_after_fenced_creation(live_shell, database, tmp_path, unit_home)

    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling' WHERE id = %s",
            (live_shell.agent_id,),
        )
        conn.commit()

    live_shell.kill(0)
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id < %s",
            (live_shell.agent_id, cutoff),
        )
        conn.commit()

    assert live_shell.list() == {}
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s",
            (live_shell.agent_id,),
        )
        assert cur.fetchall() == []

    replacement = _create(live_shell, live_host, "replacement", cwd=tmp_path)
    assert replacement == 1
    live_shell.kill(replacement)


# meiva:note
# The server-side cutoff is needed in addition to the database row lock: a client can time out
# after sending `new`, allowing the creation transaction to release its lock while the request is
# still in the service's admission queue. A terminal transition and a later resurrection can both
# commit before that delayed request is admitted. The termination fence's acknowledgement must
# therefore reject or close the old request independently of the agent's current DB status.
def test_terminal_fence_reclaims_a_delayed_lost_ack_creation_after_resurrection(
    live_shell: ShellSessions,
    live_host: SessionHost,
    database: Database,
    tmp_path: Path,
    unit_home: Path,
) -> None:
    from meiva.shell import session_plan, ttl_store

    upstream = service_socket()
    delayed_path = upstream.with_name(f"{upstream.stem}-delayed.sock")
    proxy = DelayedNewProxy(delayed_path, upstream)
    full = session_plan.session_full_name(live_shell.shell_prefix(), 0, "lost-ack")

    def start_lost_ack(_session_id: int) -> str:
        client.request(
            "new",
            socket_path=delayed_path,
            timeout=0.05,
            name=full,
            cwd=str(tmp_path),
            env={"MEIVA_HOME": str(unit_home)},
        )
        raise AssertionError("the delayed request unexpectedly returned an acknowledgement")

    def create_lost_ack_shell() -> None:
        ttl_store.create_ttl_fenced(
            database,
            live_shell.agent_id,
            120.0,
            start=start_lost_ack,
            dispose=lambda _full: None,
        )

    try:
        creator, failures = _captured_thread(create_lost_ack_shell)
        assert proxy.accepted.wait(timeout=5.0)
        creator.join(timeout=5.0)
        assert not creator.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], client.ServiceUnavailableError)

        # meiva:note
        # The timeout committed the spent id without a TTL row. Model the terminal
        # transition and resurrection that used to make this old request invisible.
        cutoff = _terminal_transition(live_shell.agent_id, database, Event(), Event())
        assert cutoff == 1
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'idling' WHERE id = %s",
                (live_shell.agent_id,),
            )
            conn.commit()

        # meiva:note
        # The terminal path receives this acknowledgement before it deletes TTL rows.
        assert client.fence_agent_shells(live_shell.agent_id, cutoff, socket_path=upstream) == []
        proxy.release.set()
        assert proxy.completed.wait(timeout=5.0)
        assert proxy.error is None
        assert proxy.response is not None and proxy.response["ok"] is False
        assert live_shell.list() == {}
        with database.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s",
                (live_shell.agent_id,),
            )
            assert cur.fetchall() == []

        replacement = _create(live_shell, live_host, "replacement", cwd=tmp_path)
        assert replacement == 1
        live_shell.kill(replacement)
    finally:
        proxy.close()


def test_renew_reads_a_legacy_long_deadline_then_replaces_it_with_a_valid_deadline(
    live_shell: ShellSessions, live_host: SessionHost, database: Database, tmp_path: Path
) -> None:
    from meiva.shell import ttl_store

    session_id = _create(live_shell, live_host, "legacy", cwd=tmp_path)
    with database.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = now() + interval '395 days' "
            "WHERE agent_id = %s AND session_id = %s",
            (live_shell.agent_id, session_id),
        )
        conn.commit()

    legacy_expiry = ttl_store.read_expiry(database, live_shell.agent_id, session_id)
    assert legacy_expiry is not None
    assert legacy_expiry > datetime.now(UTC) + timedelta(days=394)

    renewed = live_shell.renew(session_id, 120.0)
    assert renewed < legacy_expiry


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
    recovered = _create(live_shell, live_host, "recovered", cwd=tmp_path)
    assert recovered == 1
    live_shell.kill(recovered)


def test_a_session_that_cannot_start_is_refused(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="failed to create session"):
        _create(live_shell, live_host, "broken", cwd=tmp_path / "does-not-exist")

    assert live_shell.list() == {}
    recovered = _create(live_shell, live_host, "recovered", cwd=tmp_path)
    assert recovered == 1
    live_shell.kill(recovered)


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


def test_ttl_store_retries_transient_connection_failures_and_wraps_abandoned_restore(
    live_shell: ShellSessions, database: Database
) -> None:
    from typing import Any

    import psycopg

    from base.db.protocols import RestoreAbandonedError
    from meiva.shell import ttl_store

    calls = 0

    class _FlakyThenRealDb:
        def connect(self, *, autocommit: bool = False) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise psycopg.OperationalError(
                    "consuming input failed: could not receive data from server: Operation timed out"
                )
            return database.connect(autocommit=autocommit)

    flaky_db: Any = _FlakyThenRealDb()
    session_id = ttl_store.next_session_index(flaky_db, live_shell.agent_id)
    assert session_id == 0
    assert calls == 2

    calls = 0
    ttl_store.record_ttl(flaky_db, live_shell.agent_id, session_id, 120.0)
    assert calls == 2

    calls = 0
    prev_expires = ttl_store.read_expiry(flaky_db, live_shell.agent_id, session_id)
    assert prev_expires is not None
    assert calls == 2

    calls = 0
    new_expires = ttl_store.apply_renewal(
        flaky_db, live_shell.agent_id, session_id, 180.0, prev_expires
    )
    assert new_expires > prev_expires
    assert calls == 2

    class _AlwaysAbandonedDb:
        def connect(self, *, autocommit: bool = False) -> Any:
            del autocommit
            raise RestoreAbandonedError("session restore abandoned")

    abandoned_db: Any = _AlwaysAbandonedDb()
    with pytest.raises(RuntimeError, match="failed to renew session 0"):
        ttl_store.read_expiry(abandoned_db, live_shell.agent_id, session_id)
    with pytest.raises(RuntimeError, match="failed to renew session 0"):
        ttl_store.apply_renewal(abandoned_db, live_shell.agent_id, session_id, 180.0, new_expires)
