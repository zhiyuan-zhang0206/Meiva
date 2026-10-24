"""meiva.watcher unit tests — launch writes the agent's script verbatim plus a
generated bootstrap file (identity + watchdog + runpy), and runs them in a
PTY session whose command line tees output to a log file and session capture,
then ends with the CLI completion notice (`meiva agents send ... --source
watcher:N`); cron/at build a script then spawn.

The `pty_service` fixture (base/sessions/pty/tests/pty_service.py) runs a real
pty-sessions service under the tmp test home."""

from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

import psycopg
import pytest

import meiva
from base.sessions.pty.tests.pty_shells import wait_for
from meiva import watcher
from meiva.watcher import launch_plan, plans, spawn, wiring
from meiva.watcher.spawn import SpawnDeps

pytestmark = [
    # `_isolated_agent` is opt-in (mutates global meiva.self.AGENT_ID); apply it
    # module-wide here since every watcher session test needs the fake-id +
    # pty cleanup isolation. `pty_service` first — the isolation fixture's
    # own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("sdk_model_owner", "pty_service", "_isolated_agent"),
]


def _is_live_watcher(wid: int, name: str = "test-watcher") -> bool:
    return meiva.shell.sessions.list().get(wid) == name


def _watchers_dir() -> pathlib.Path:
    """Where the bound agent's generated watcher files go."""
    return wiring.spawn_deps().watchers_dir


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _launch(*args: Any, **kwargs: Any) -> int:
    return watcher.launch(*args, **kwargs).session_id


def _cron(*args: Any, **kwargs: Any) -> int:
    return watcher.cron(*args, **kwargs).session_id


def _at(*args: Any, **kwargs: Any) -> int:
    return watcher.at(*args, **kwargs).session_id


def test_launch_creates_watcher_session(_agent_row: int) -> None:
    # A long-running watcher keeps its session alive and listed under its name.
    code = "import time\ntime.sleep(60)\n"
    wid = _launch(code, timeout="1h", name="test-launch")
    try:
        assert isinstance(wid, int)
        assert _is_live_watcher(wid, "test-launch")
    finally:
        meiva.shell.sessions.kill(wid)


def _ttl_deadline(
    conn: psycopg.Connection, agent_id: int, session_id: int
) -> datetime.datetime | None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def test_launch_registers_ttl_row_from_timeout(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #2614: a launch watcher records its timeout as the session TTL —
    the shell monitor page must show a deadline for every session."""
    before = datetime.datetime.now(datetime.UTC)
    wid = _launch("import time\ntime.sleep(60)\n", timeout="30m", name="test-launch-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert (
            before + datetime.timedelta(minutes=29)
            <= deadline
            <= datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=31)
        )
    finally:
        meiva.shell.sessions.kill(wid)


def test_cron_registers_ttl_folded_to_end_time(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: a cron watcher's session TTL IS its deadline — the
    registration's end_time (defaulted to now + 7 days, task #2617), folded
    as `deadline - now` at mount time — never the old 24h placeholder."""
    before = datetime.datetime.now(datetime.UTC)
    wid = _cron("0 4 * * *", "wake", name="test-cron-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        delta = deadline - before
        # Default end = the registration minute + 7 days (minute-truncated).
        assert datetime.timedelta(days=7) - datetime.timedelta(minutes=1) <= delta
        assert delta <= datetime.timedelta(days=7, minutes=1)
        # The system-side true value is exempt from the 24h sessions.new cap.
        assert deadline > datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=6)
    finally:
        meiva.shell.sessions.kill(wid)


def test_cron_registers_ttl_from_explicit_end(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """An explicit end_time beyond the user-session cap still folds exactly:
    the watcher TTL is the stored deadline, uncapped (task #3411)."""
    end = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=30)
    wid = _cron("0 4 * * *", "wake", end_time=end, name="test-cron-ttl-explicit")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert abs((deadline - end).total_seconds()) < 10
    finally:
        meiva.shell.sessions.kill(wid)


def test_launch_timeout_beyond_24h_is_not_capped(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: a launch watcher's timeout IS its deadline — a 48h
    watchdog registers a ~48h session TTL. The 24h cap protects user sessions
    (sessions.new / run_background), not system watchers."""
    wid = _launch("import time\ntime.sleep(60)\n", timeout="48h", name="test-cap-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert deadline >= datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=47, minutes=59
        )
    finally:
        meiva.shell.sessions.kill(wid)


def test_at_registers_ttl_row_from_fires_at_plus_grace(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: an at watcher's session TTL is its fire moment plus the
    grace (`base.daemon.schedules.watcher.AT_SESSION_TTL_GRACE_SECONDS`), so the wake
    delivery and the session's exit notice complete before the reaper may
    reclaim it."""
    from base.daemon.schedules.watcher import AT_SESSION_TTL_GRACE_SECONDS

    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
    wid = _at(when, "wake", name="test-at-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        expected = when + datetime.timedelta(seconds=AT_SESSION_TTL_GRACE_SECONDS)
        assert abs((deadline - expected).total_seconds()) < 10
    finally:
        meiva.shell.sessions.kill(wid)


def test_watcher_child_dies_when_the_pty_service_dies(
    _agent_row: int, tmp_path: pathlib.Path
) -> None:
    """Task #1726 acceptance: a watcher child must NEVER outlive its pty service.

    When the service dies (crash / SIGKILL), the login shell dies with it
    (the master closes, which hangs it up) and the child is reparented to
    init — still alive, still firing cron/at; 49 of 85 watcher processes on
    the fleet host were such multi-generation orphans (8/19 onwards). The
    bootstrap's orphan guard
    compares getppid() against the boot-time parent and hard-exits within a
    few seconds. The child IGNORES SIGHUP, so its death can only come from
    the guard — the test fails if the guard regresses and the child survives.
    """
    import signal
    from contextlib import suppress

    import psutil

    from meiva.shell import sessions as _sessions

    pidfile = tmp_path / "child.pid"
    pending_pidfile = tmp_path / "child.pid.pending"
    code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"with open({str(pending_pidfile)!r}, 'w') as _pidfile:\n"
        "    _pidfile.write(str(os.getpid()))\n"
        f"os.replace({str(pending_pidfile)!r}, {str(pidfile)!r})\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    wid = _launch(code, timeout="1h", name="test-orphan-guard")

    deadline = time.time() + 20
    # The child publishes the ready file atomically after its contents are
    # closed, so existence means the PID is complete rather than merely open.
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.2)
    assert pidfile.exists(), "watcher child never started"
    child_pid = int(pidfile.read_text())

    try:
        # The child's parent is the session shell; the grandparent is the pty
        # service — the process whose death orphans the child.
        child = psutil.Process(child_pid)
        shell = psutil.Process(child.ppid())
        host = psutil.Process(shell.ppid())
        assert "services.agent_runner.pty_sessions.daemon" in " ".join(host.cmdline())

        os.kill(host.pid, signal.SIGKILL)

        # The guard exits within its 5s interval; 20s is generous CI slack.
        deadline = time.time() + 20
        while time.time() < deadline and psutil.pid_exists(child_pid):
            try:
                if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                    break  # dead, waiting for init to reap
            except psutil.NoSuchProcess:
                break
            time.sleep(0.2)
        assert (
            not psutil.pid_exists(child_pid)
            or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        ), "watcher child survived its host"
    finally:
        # A failed assertion must not leak the sleeper (the session record is
        # gone with the host, so fixture teardown cannot reach it).
        with suppress(psutil.Error):
            psutil.Process(child_pid).kill()
        with suppress(ValueError, subprocess.CalledProcessError):
            _sessions.kill(wid)


def test_launch_requires_timeout_and_name() -> None:
    # Both timeout and name are required
    with pytest.raises(TypeError):
        _launch("import meiva\n")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _launch("import meiva\n", timeout="1h")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _launch("import meiva\n", name="test")  # type: ignore[call-arg]


@pytest.mark.flaky  # real watcher session + time.sleep polling (5s deadline)
def test_shell_kill_stops_watcher(_agent_row: int) -> None:
    # A watcher is an ordinary session — the generic session kill stops it.
    code = "import time\ntime.sleep(60)\n"
    wid = _launch(code, timeout="1h", name="test-launch")
    meiva.shell.sessions.kill(wid)
    # Poll instead of a fixed sleep: session teardown latency varies under CPU
    # contention (audit round-2 cc-docs-tests P2).
    deadline = time.time() + 5
    while time.time() < deadline and _is_live_watcher(wid, "test-launch"):
        time.sleep(0.05)
    assert not _is_live_watcher(wid, "test-launch")


# meiva:note
# A session whose launch never sent must not linger as an idle login shell under the watcher's
# name until its TTL (up to 7 days for a default cron): it is killed and the failure propagates.
# The failure is a real one: the path of one generated file is already taken by a directory.
@pytest.mark.parametrize("generated", ["{}.py", "{}_boot.py", "{}_launch.sh"])
def test_spawn_kills_session_when_the_launch_fails(
    _agent_row: int, spawn_deps: SpawnDeps, generated: str
) -> None:
    probe = meiva.shell.sessions.new("probe", ttl=300)
    next_id = probe + 1
    blocked = spawn_deps.watchers_dir / generated.format(f"watcher_{next_id}")
    blocked.mkdir()
    try:
        with pytest.raises(IsADirectoryError):
            spawn.run(
                plans.plan_launch("import meiva\n", "1h", "test-fail", None, _now()), spawn_deps
            )

        assert next_id not in meiva.shell.sessions.list()
    finally:
        blocked.rmdir()
        meiva.shell.sessions.kill(probe)


def test_watcher_rejects_unknown_notify(_agent_row: int) -> None:
    with pytest.raises(ValueError, match="notify must be one of"):
        _launch("pass", timeout="1h", name="test-invalid-notify", notify="success")


def test_launch_writes_the_generated_files_while_the_watcher_runs(
    _agent_row: int, spawn_deps: SpawnDeps
) -> None:
    code = "from __future__ import annotations\nimport time\ntime.sleep(60)\n"

    handle = spawn.run(plans.plan_launch(code, "1h", "test-files", None, _now()), spawn_deps)

    try:
        wid = handle.session_id
        assert (spawn_deps.watchers_dir / f"watcher_{wid}.py").read_text() == code
        assert (
            f"watcher_{wid}.py" in (spawn_deps.watchers_dir / f"watcher_{wid}_boot.py").read_text()
        )
    finally:
        meiva.shell.sessions.kill(handle.session_id)


# -- Completion notice (shell-level, fires on every exit path) -----------------


@pytest.mark.flaky  # real pty watcher session + time.sleep polling (15s deadline)
def test_watcher_completion_notice_e2e(
    _agent_row: int, spawn_deps: SpawnDeps, fake_cli: pathlib.Path
) -> None:
    """End-to-end through a real pty session: when the watcher child exits, the
    completion notice fires (CLI invoked with the watcher source) and the
    session closes itself. The CLI is faked with a script that records its
    argv, so no gateway is needed."""
    argv_file = fake_cli.parent / "argv.txt"

    # `exit 7` exits the shell subshell wrapping the python command — use a
    # python-level SystemExit instead so the exit code flows through the child.
    wid = spawn.run(
        plans.plan_launch("raise SystemExit(7)\n", "1h", "test-e2e", None, _now()),
        dataclasses.replace(spawn_deps, cli=fake_cli),
    ).session_id

    deadline = time.time() + 15
    while time.time() < deadline and not argv_file.exists():
        time.sleep(0.3)
    assert argv_file.exists(), "completion notice never fired"
    argv = argv_file.read_text().splitlines()
    assert argv[0] == "agents"
    assert argv[1] == "send"
    assert argv[2] == str(meiva.self.AGENT_ID)
    assert "finished" in argv[3]
    assert "exited with code" not in argv[3]
    assert "--source" in argv
    assert f"watcher:{wid}" in argv

    # The session closes itself after the notice is delivered.
    deadline = time.time() + 10
    while time.time() < deadline and wid in meiva.shell.sessions.list():
        time.sleep(0.3)
    assert wid not in meiva.shell.sessions.list()


def test_watcher_launch_during_shell_startup_with_long_paths(
    _agent_row: int,
    tmp_path: pathlib.Path,
    unit_home: pathlib.Path,
    spawn_deps: SpawnDeps,
) -> None:
    """Long notice paths must survive the shell's startup canonical input mode."""
    # Delay readline startup deterministically; a new PTY is initially canonical.
    (unit_home / ".bash_profile").write_text("sleep 1\n")
    # Darwin's canonical input limit is smaller than Linux's; stay below
    # each platform's filesystem path limit while exceeding its input line.
    depth = 1 if sys.platform == "darwin" else 6
    carrier_dir = tmp_path / "carrier with 'quotes'"
    carrier_dir.mkdir()
    long_dir = tmp_path.joinpath(*("long-path-" + "a" * 180 for _ in range(depth)))
    long_dir.mkdir(parents=True)
    argv_file = long_dir / "argv.txt"
    fake_cli = long_dir / "fake-meiva"
    fake_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_file}'\n")
    fake_cli.chmod(0o755)
    wid = spawn.run(
        plans.plan_launch("raise SystemExit(7)\n", "1h", "test-long-path", None, _now()),
        dataclasses.replace(spawn_deps, watchers_dir=carrier_dir, workspace=long_dir, cli=fake_cli),
    ).session_id
    assert wait_for(argv_file.exists, timeout=15), meiva.shell.sessions.capture(wid, lines=30)
    argv = argv_file.read_text().splitlines()
    assert "finished." in argv[3]
    assert f"watcher:{wid}" in argv
    output_path = long_dir / ".shell_logs" / f"{wid}_test-long-path.log"
    assert f"Full output at {output_path}." in argv[3]
    assert str(output_path) == argv[argv.index("--tail-file") + 1]
    assert wait_for(lambda: wid not in meiva.shell.sessions.list(), timeout=10)
    assert not (carrier_dir / f"watcher_{wid}_launch.sh").exists()
    assert not (carrier_dir / f"watcher_{wid}_boot.py").exists()
    assert not (carrier_dir / f"watcher_{wid}.py").exists()


# -- Agent identity in the child (Task #964 regression) ----------------------


@pytest.mark.flaky  # real pty watcher session + file-polling (15s deadline)
def test_watcher_child_sees_agent_identity(
    _agent_row: int, tmp_path: pathlib.Path, spawn_deps: SpawnDeps, fake_cli: pathlib.Path
) -> None:
    """End-to-end regression (Task #964): a real watcher child must see its
    agent id via meiva.self.AGENT_ID — the wake-up send_message depends on it.
    Before the fix the child had no MEIVA_AGENT_ID (None) and every scheduled
    watcher died with a 422 on /api/agents/None/messages."""
    from meiva.shell import sessions as _sessions

    out = tmp_path / "identity.txt"
    code = (
        "import meiva\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(meiva.self.AGENT_ID))\n"
    )
    wid = spawn.run(
        plans.plan_launch(code, "1h", "test-identity", None, _now()),
        dataclasses.replace(spawn_deps, cli=fake_cli),
    ).session_id

    deadline = time.time() + 15
    while time.time() < deadline and not out.exists():
        time.sleep(0.3)
    assert out.exists(), "watcher child never wrote its identity"
    assert out.read_text() == str(_agent_row)  # NOT the stale session-env id
    # The watcher self-terminates and the session closes itself after the
    # completion notice; if the child is somehow still alive, clean it up but
    # tolerate an already-closed session (the kill path resolves by name).
    from contextlib import suppress

    # Tolerate every "session is already gone" failure mode: `_resolve` raises
    # ValueError for a session that is not ours, and the pty kill path
    # raises subprocess.CalledProcessError when the session vanished mid-kill
    # (exit 1 from kill-session) — a teardown must not fail on a corpse.
    with suppress(ValueError, subprocess.CalledProcessError):
        _sessions.kill(wid)


def test_boot_overrides_stale_env_identity(tmp_path: pathlib.Path) -> None:
    """The generated bootstrap must override a stale MEIVA_AGENT_ID in the
    environment: `import meiva` establishes identity FROM THE ENV at import
    time, so a session whose env carries another agent's id (a server
    freezes the env of its first session) would otherwise bind the WRONG
    identity and every wake-up send_message would route there. Regression for
    2026-08-09 — every watcher child on the shared server woke agent 2959
    instead of its owner. Runs the boot file in a real subprocess with the
    env poisoned; the child must report the INLINED id, not the stale one."""
    script = tmp_path / "probe.py"
    out = tmp_path / "identity.txt"
    script.write_text(
        "import meiva\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(meiva.self.AGENT_ID))\n"
    )
    boot_path = tmp_path / "probe_boot.py"
    boot_path.write_text(launch_plan.build_boot(script, None, 4242))

    env = {**os.environ, "MEIVA_AGENT_ID": "9999"}  # stale/wrong identity
    res = subprocess.run(  # noqa: S603 — repo-internal generated boot file
        [sys.executable, str(boot_path)],
        cwd=pathlib.Path(__file__).resolve().parents[3],  # repo root: `import meiva` resolves
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, f"boot failed: {res.stderr}"
    assert out.read_text() == "4242", "the stale env identity won over the inline id"


def test_watcher_child_overrides_stale_session_identity(
    _agent_row: int, tmp_path: pathlib.Path, spawn_deps: SpawnDeps, fake_cli: pathlib.Path
) -> None:
    """A stale MEIVA_AGENT_ID in the session env must NOT win: the bootstrap
    inlines the spawning agent's id BEFORE `import meiva`. Regression for
    2026-08-09 — every watcher child on the shared supervisor woke agent 2959
    instead of its owner, because the server's frozen env carried 2959 and
    `import meiva` established it before the inline assignment ran. Here the
    session env is poisoned with a wrong id (via the backend's envfile); the
    child must still report the real agent id."""
    from meiva.shell import sessions as _sessions

    out = tmp_path / "identity.txt"
    code = (
        "import meiva\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(meiva.self.AGENT_ID))\n"
    )
    stale = _agent_row + 1  # a WRONG identity, as if frozen into the session env
    wid = spawn.run(
        plans.plan_launch(code, "1h", "test-stale-id", None, _now()),
        dataclasses.replace(
            spawn_deps,
            cli=fake_cli,
            runner_env={**spawn_deps.runner_env, "MEIVA_AGENT_ID": str(stale)},
        ),
    ).session_id

    deadline = time.time() + 15
    while time.time() < deadline and not out.exists():
        time.sleep(0.3)
    assert out.exists(), "watcher child never wrote its identity"
    assert out.read_text() == str(_agent_row), "the stale session identity won"
    from contextlib import suppress

    # Tolerate every "session is already gone" failure mode: `_resolve` raises
    # ValueError for a session that is not ours, and the pty kill path
    # raises subprocess.CalledProcessError when the session vanished mid-kill
    # (exit 1 from kill-session) — a teardown must not fail on a corpse.
    with suppress(ValueError, subprocess.CalledProcessError):
        _sessions.kill(wid)


# ─── watchers carry no registry; nothing dedupes or restarts them ───────────
# (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md)


def test_cron_registered_twice_yields_two_independent_sessions(_agent_row: int) -> None:
    """Calling cron() again with the same expression + timezone no longer
    dedupes, renews, or supersedes anything — it starts a second, independent
    session, and both keep running until killed."""
    first = _cron("0 4 * * *", "wake", timezone="UTC", name="test-cron-dup-1")
    second = _cron("0 4 * * *", "wake", timezone="UTC", name="test-cron-dup-2")
    try:
        assert first != second
        alive = meiva.shell.sessions.list()
        assert first in alive
        assert second in alive
    finally:
        meiva.shell.sessions.kill(first)
        meiva.shell.sessions.kill(second)
