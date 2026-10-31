"""ScheduleManager x real pty-sessions service integration.

A real pty-sessions service + real DB + real backend + the real
`services.wake.schedule_manager.runner` entrypoint: `_launch` creates a session in the service,
`_live_ids` / `capture_blocking` see it, `_reap` tears it down, and a failed
revision is never launched again after manager restart.

The schedule commands are deliberately trivial (`sleep 30` for the live
paths, `false` for the crash paths) — the runner's real script execution and
status semantics are covered by test_schedule_runner.py.
"""

import shlex
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.deploy.lifecycle.start_serving import RootBirth
from base.deploy.lifecycle.tests.serving_root import observe
from base.host.env.registry import process_environment
from base.paths import meiva_home
from base.sessions.backend import get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import PtyServiceProcess, service_socket
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import wait_for
from gateway.schedules import session_control
from gateway.schedules.tests.db_faults import failing_write
from services.wake.schedule_manager import manager as sm
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager

REPO = Path(__file__).resolve().parents[4]

pytestmark = [
    pytest.mark.usefixtures("_pty_home"),
]


@pytest.fixture
def _pty_home(unit_home: Path, db_url: str, pty_service: PtyServiceProcess) -> str:
    """The test's home, with its own pty-sessions service, for the schedule sessions.

    The service is a subprocess pinned to this home; each schedule launch creates a session in
    it. The runner is a separate process that builds its settings from the home's `.env`: name
    the test database there so a `sleep 30` schedule stays up long enough to observe (the
    runner's own semantics are covered by test_schedule_runner.py). The `pty_service` fixture
    closes every session and stops the service, so no shell outlives the test run.
    """
    del pty_service
    (unit_home / ".env").write_text(f"MEIVA_DB_URL={db_url}\n")
    return str(unit_home)


@pytest.fixture
def pool(db_url: str, _host_is_serving: None) -> Iterator[ConnectionPool[psycopg.Connection]]:
    from base.config import settings

    p: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield p
    finally:
        p.close()


def _insert_schedule(
    conn: psycopg.Connection,
    name: str,
    script: str,
    command: str,
    *,
    enabled: bool = True,
    machine: str = "gateway",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, machine) "
            "VALUES (%s, %s, %s, %s, 'stopped', %s) RETURNING id",
            (name, script, command, enabled, machine),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


@pytest.fixture
def _host_is_serving(serving_root: RootBirth, _pty_home: str) -> Iterator[None]:
    """PTY schedule cases model a gateway that completed its start gate."""
    from base.deploy.lifecycle import start_serving
    from base.paths import meiva_home

    home = meiva_home()
    generation = start_serving.begin_start(home)
    assert (
        start_serving.mark_serving(home, generation, runtime=serving_root.runtime, observe=observe)
        is True
    )
    try:
        yield
    finally:
        start_serving.clear_serving(home)


# meiva:note
# A manager launching the way the daemon does: the real session backend and the forwarded env.
def _manager(pool: ConnectionPool, *, repo_root: Path = REPO) -> sm.ScheduleManager:
    return schedule_manager(
        pool,
        get_shell_backend(home=meiva_home()),
        repo_root=repo_root,
        launch_env=forward_env_dict(process_environment()),
    )


def _dump_logs(home: Path) -> str:
    """Diagnosis aid for CI-only failures: every session transcript + the service log."""
    diag: list[str] = []
    for p in [*sorted((home / "logs").glob("*.out.log")), home / "pty-sessions-service.log"]:
        if p.exists():
            diag.append(f"--- {p.name} ---\n" + p.read_text(errors="replace")[-1200:])
    return "\n".join(diag)


def _wait_session_gone(name: str, timeout_s: float = 20.0) -> None:
    backend = get_shell_backend(home=meiva_home())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and backend.has_session(name):
        time.sleep(0.3)
    assert not backend.has_session(name), f"session {name} still live"


def test_launch_cwd_is_the_checkout_root() -> None:
    """Schedule sessions start in the checkout root, where `python -m
    services.wake.schedule_manager.runner` and the relative `.venv/bin/python` resolve."""
    assert sm.REPO_ROOT == REPO


def test_pty_launch_live_capture(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    _pty_home: str,
) -> None:
    """_launch through the real PTY backend: the session is live in the service,
    _live_ids sees it, capture returns the runner's output."""
    # A real .py script (command must name the script file — a bare command
    # with no extension made the runner execute the script body as .py and
    # crash instantly; round 4 CI, 2893). The print marks real execution so
    # the assertion cannot false-pass on a traceback's module path.
    sid = _insert_schedule(
        db_conn,
        "pty-live",
        "import time; from meiva.sdk_surface.agent_identity import require_actor; "
        "print('RUNNER_ALIVE', require_actor()); time.sleep(30)",
        "python schedule.py",
    )
    mgr = _manager(pool)
    mgr.reconcile(now=NOW)

    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend(home=meiva_home())
    assert backend.has_session(name)
    assert name in [info.name for info in client.list_sessions(socket_path=service_socket())], (
        "service does not list it"
    )
    assert sid in mgr._live_ids(NOW)
    # Poll for the runner's output instead of one immediate capture: under a
    # slow CI login shell the launch-to-output window is variable, and a
    # single probe can land before the command is delivered or after the
    # runner has already exited (2893). A session that dies mid-poll dumps
    # the session logs — the runner's traceback lands in the .out.log.
    home = Path(_pty_home)
    captured: str | None = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            captured = session_control.capture_blocking(
                get_shell_backend(home=meiva_home()), sid, 50
            )
        except Exception as exc:
            raise AssertionError(
                f"capture failed ({exc}); session logs:\n{_dump_logs(home)}"
            ) from exc
        if captured and f"RUNNER_ALIVE schedule:{sid}" in captured:
            break
        time.sleep(0.5)
    if not (captured and f"RUNNER_ALIVE schedule:{sid}" in captured):
        raise AssertionError("runner output never appeared; session logs:\n" + _dump_logs(home))

    assert mgr._reap(sid, NOW)
    _wait_session_gone(name)


def test_pty_crash_is_not_replayed_after_manager_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, _pty_home: str
) -> None:
    """A real script effect occurs once; fresh managers never repeat it."""
    marker = Path(_pty_home) / "crash-effects"
    script = f"printf 'effect\\n' >> {marker}; exit 1"
    sid = _insert_schedule(db_conn, "pty-crash", script, "bash run.sh")
    mgr = _manager(pool)
    mgr.reconcile(now=NOW)
    name = session_name(f"schedule-{sid}")
    backend = get_shell_backend(home=meiva_home())
    assert backend.has_session(name)

    # The runner exits nonzero immediately → session ends on its own.
    _wait_session_gone(name)

    for _ in range(3):
        _manager(pool).reconcile(now=NOW)
        assert not backend.has_session(name)
    row = db_conn.execute(
        "SELECT status, launch_count FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert row == ("error", 1)
    assert marker.read_text().splitlines() == ["effect"]

    # An explicit restart is a new revision and deliberately authorizes a new effect.
    db_conn.execute(
        "UPDATE schedules SET desired_revision = desired_revision + 1, "
        "launch_count = 0, status = 'stopped' WHERE id = %s",
        (sid,),
    )
    db_conn.commit()
    assert _manager(pool).sync_one(sid, now=NOW)
    _wait_session_gone(name)
    _manager(pool).reconcile(now=NOW)
    assert marker.read_text().splitlines() == ["effect", "effect"]


def test_real_pty_revision_is_adopted_after_lost_acknowledgement(
    db_conn: psycopg.Connection, pool: ConnectionPool, _pty_home: str, tmp_path: Path
) -> None:
    from services.wake.schedule_manager import revisions

    # A real PTY runner stub holds the exact pre-acknowledgement window open;
    # the ordinary runner would also write running during its own startup.
    marker = tmp_path / "runner-births"
    runner = tmp_path / ".venv" / "bin" / "python"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        f"#!/bin/sh\nprintf 'birth\\n' >> {shlex.quote(str(marker))}\nexec sleep 30\n"
    )
    runner.chmod(0o755)
    sid = _insert_schedule(
        db_conn, "pty-revision", "import time; time.sleep(30)", "python schedule.py"
    )
    db_conn.execute("UPDATE schedules SET desired_revision = 1 WHERE id = %s", (sid,))
    db_conn.commit()
    name = session_name(f"schedule-{sid}")
    mgr = _manager(pool, repo_root=tmp_path)
    try:
        # meiva:note
        # The write that acknowledges the running process is the one that fails: the process
        # was born, the manager lost its acknowledgement.
        with (
            failing_write(
                db_conn, table="schedules", events="UPDATE", when="NEW.status = 'running'"
            ),
            pytest.raises(psycopg.errors.RaiseException, match="injected fault"),
        ):
            mgr.sync_one(sid, now=NOW)
        assert revisions.launched_revision(meiva_home(), sid) == 1, repr(
            client.live_sessions(name, socket_path=service_socket())
        )
        before = get_shell_backend(home=meiva_home()).session_started_at(name)
        assert before is not None
        assert wait_for(marker.exists)
        assert db_conn.execute(
            "SELECT status, launch_count FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == ("error", 1)
        _manager(pool, repo_root=tmp_path).reconcile(now=NOW)
        assert get_shell_backend(home=meiva_home()).session_started_at(name) == before
        assert db_conn.execute(
            "SELECT status, launch_count, last_error FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == ("running", 1, None)
        assert marker.read_text().splitlines() == ["birth"]
    finally:
        mgr._reap(sid, NOW)
        _wait_session_gone(name)
