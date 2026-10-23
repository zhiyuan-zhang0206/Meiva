"""Start fresh serve() registrations once and observe their persistent sessions.

A live session survives daemon restarts. Lost sessions and failed page servers
close their page records without relaunch or agent wake. A new explicit serve
registration is required to create another server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import signal
import sys
import time
import urllib.request
from collections.abc import Callable, Coroutine, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import psycopg
from psycopg_pool import ConnectionPool

from base.clock import SYSTEM_TIMERS
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.host.exit_codes import hard_exit as _hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home, run_dir
from base.sessions.backend import get_shell_backend
from base.sessions.page_session import page_session_name
from base.sessions.protocol import SessionBackend
from base.sessions.pty import client as pty_client
from base.sessions.pty.paths import service_socket_path
from base.sessions.record import SessionRecord
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.agent_runner.page_server import resources
from services.agent_runner.page_server.config import PageServerConfig

from . import dead_pages
from .degradation import PageRow, close_failed_page

_log = logging.getLogger("services.agent_runner.page_server.daemon")

_LIVENESS_TIMEOUT_S = 60.0
# A dead-page round probes every open show page of the host (1.5 s each, eight at once);
# a loop that completed nothing for this long is wedged.
_DEAD_PAGES_LIVENESS_TIMEOUT_S = 600.0


def page_server_config(*, daemon_settings: Any | None = None) -> PageServerConfig:
    """The composition root: the one place this package reads `settings`."""
    source = settings.daemon if daemon_settings is None else daemon_settings
    return PageServerConfig(
        page_server_poll_interval_seconds=source.page_server_poll_interval_seconds,
        heartbeat_interval_seconds=source.heartbeat_interval_seconds,
    )


# A new pty session needs a short window to finish its interactive-shell startup
# and receive its initial command before a health probe can make a decision.
_SPAWN_VERIFY_TIMEOUT_S = 5.0
PAGE_SERVER_MODULE = "services.agent_runner.page_server.server"


@dataclass
class ServerHandle:
    """One page-server command supervised through its persistent shell."""

    agent_id: int
    name: str
    port: int
    serve_dir: str
    token: str = field(repr=False)
    session_name: str
    last_launch_monotonic: float


def open_rows(pool: ConnectionPool, host: str) -> list[PageRow]:
    """Every open serve row supervised by this host's daemon."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, agent_id, name, port, host, serve_dir, server_token, session_name "
            "FROM agent_pages "
            "WHERE closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NOT NULL AND host = %s "
            "ORDER BY agent_id, name",
            (host,),
        )
        return [PageRow(*row) for row in cur.fetchall()]


def _closed_page_sessions(pool: ConnectionPool, host: str) -> list[str]:
    """Persisted page sessions whose rows closed while this daemon was away."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_name FROM agent_pages "
            "WHERE (closed_at IS NOT NULL OR expired_at IS NOT NULL) "
            "AND serve_dir IS NOT NULL AND host = %s "
            "AND session_name IS NOT NULL",
            (host,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _server_command(row: PageRow) -> str:
    """The shell-safe command that starts a row's page server."""
    return shlex.join(
        [
            sys.executable,
            "-m",
            PAGE_SERVER_MODULE,
            "--port",
            str(row.port),
            "--host",
            row.host,
            "--dir",
            row.serve_dir,
        ]
    )


def _allocate_session_index(pool: ConnectionPool, agent_id: int) -> int:
    """Atomically allocate one shell id from the owning agent's shared counter."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET session_index = session_index + 1 "
            "WHERE id = %s RETURNING session_index",
            (agent_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError(f"agent {agent_id} not in agents_meta table — cannot allocate session")
    return int(row[0]) - 1


def _ensure_token(pool: ConnectionPool, row: PageRow) -> str:
    """Return the row's durable health token, minting it on first adoption.

    The mint only persists for a still-open row; a row that closed or expired
    while this pass reconciled is terminal and never written (the caller
    drops it this pass anyway), so the minted value is returned unpersisted
    rather than raising and aborting the whole reconcile pass."""
    if row.server_token is not None:
        return row.server_token
    minted = secrets.token_hex(16)
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET server_token = COALESCE(server_token, %s) "
            "WHERE id = %s AND port = %s AND host = %s AND serve_dir = %s "
            "AND closed_at IS NULL AND expired_at IS NULL "
            "RETURNING server_token",
            (minted, row.id, row.port, row.host, row.serve_dir),
        )
        persisted = cur.fetchone()
        conn.commit()
    if persisted is None or persisted[0] is None:
        return minted
    return str(persisted[0])


# meiva:note
# Undo THIS pass's reservation, and only that one: `session_name = <ours>` is the
# compare half. A concurrent reconcile (or the operator's own re-serve) may have
# written a newer name between the claim and this failure, and clearing that row
# would delete a session somebody else is about to create.
def _release_claim(pool: ConnectionPool, row: PageRow, page_session: str, token: str) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE agent_pages SET session_name = NULL WHERE id = %s AND session_name = %s "
            "AND server_token = %s AND closed_at IS NULL AND expired_at IS NULL",
            (row.id, page_session, token),
        )


def _create_page_session(
    pool: ConnectionPool, backend: SessionBackend, row: PageRow, token: str
) -> str:
    """Reserve a fresh registration before creating its shell once."""
    page_session = row.session_name
    if page_session is None:
        page_session = page_session_name(
            row.agent_id, row.name, _allocate_session_index(pool, row.agent_id)
        )
    with write_transaction(pool) as conn:
        claimed = conn.execute(
            "UPDATE agent_pages SET session_name = %s WHERE id = %s "
            "AND port = %s AND serve_dir = %s AND server_token = %s AND host = %s "
            "AND session_name IS NULL AND closed_at IS NULL AND expired_at IS NULL",
            (page_session, row.id, row.port, row.serve_dir, token, row.host),
        ).rowcount
    if claimed != 1:
        raise RuntimeError(f"page row {row.id} changed before initial session admission")
    env = {**os.environ, "PAGE_SERVER_TOKEN": token}
    if not backend.new_session(
        page_session,
        _server_command(row),
        Path(row.serve_dir),
        env=env,
    ):
        _release_claim(pool, row, page_session, token)
        raise RuntimeError(f"failed to create page session {page_session!r}")
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET session_name = %s "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir = %s AND host = %s AND port = %s "
            "AND session_name = %s AND server_token = %s",
            (page_session, row.id, row.serve_dir, row.host, row.port, page_session, token),
        )
        if cur.rowcount == 0:
            # The row left the open set between the liveness re-check and this
            # write (closed, expired, or re-registered without serve_dir) —
            # the caller's snapshot can be seconds stale. A session without a
            # live row is a recordless ghost the next pass would reap as an
            # orphan; roll the just-created session back instead.
            _log.warning(
                "[page-server] page row %s left the open set while creating its session; "
                "rolling back %s",
                row.id,
                page_session,
            )
            backend.kill_session(page_session)
            raise RuntimeError(f"page row {row.id} left the open set while creating its session")
        conn.commit()
    return page_session


def server_is_healthy(host: str, port: int, token: str) -> bool:
    """Whether the server answers /health with this row's durable token."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            return resp.read() == f"ok:{token}".encode()
    except OSError:
        return False


def page_server_occupants(
    *, process_iter_fn: Callable[[], Iterable[Any]] = psutil.process_iter
) -> dict[int, tuple[int, str | None]]:
    """Map each local page-server port to its process and cluster home."""
    found: dict[int, tuple[int, str | None]] = {}
    for proc in process_iter_fn():
        try:
            raw_cmdline = proc.cmdline()
            cmdline = [str(part) for part in raw_cmdline] if raw_cmdline else []
            pid = int(proc.pid)
        except (psutil.Error, OSError, SystemError, TypeError, ValueError):
            # One unreadable process must not end the scan: psutil 7.2.2 on
            # macOS raised SystemError reading some cmdlines, and as an attrs
            # read inside process_iter itself it escaped this handler and
            # aborted the whole poll round — that round's stale cleanup and
            # relaunch were skipped (task #4964).
            continue
        rendered = " ".join(cmdline)
        if PAGE_SERVER_MODULE not in rendered:
            continue
        match = re.search(r"--port (\d+)", rendered)
        if match is None:
            continue
        try:
            raw_env = proc.environ()
            env: dict[str, str] = dict(raw_env) if raw_env else {}
        except (psutil.Error, OSError):
            env = {}
        found[int(match.group(1))] = (pid, env.get("MEIVA_HOME"))
    return found


def live_session_records() -> dict[str, SessionRecord]:
    """One listing of the live pty sessions' records.

    A reconcile pass that asked ``has_session`` once per managed row would
    serialize a round trip per row inside the 2 s poll (the 2026-08-28 incident,
    when each ask cost a Python startup and serve()'s 15 s wait timed out about
    half the time); one listing answers every row.
    """
    return pty_client.live_sessions(socket_path=service_socket_path(run_dir(meiva_home())))


def page_session_shell_pids(
    wanted: dict[tuple[int, str], PageRow], live: dict[str, SessionRecord]
) -> dict[int, tuple[int, str]]:
    """Map live page-shell PIDs to their page-row keys.

    ``live`` is the pass's pty record scan."""
    session_keys = {
        row.session_name: key for key, row in wanted.items() if row.session_name is not None
    }
    return {record.pid: session_keys[name] for name, record in live.items() if name in session_keys}


def page_session_owner(
    pid: int,
    shell_pids: dict[int, tuple[int, str]],
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
) -> tuple[int, str] | None:
    """Return the page row whose live shell is an ancestor of ``pid``."""
    try:
        parents = process_factory(pid).parents()
    except psutil.Error:
        return None
    for parent in parents:
        if owner := shell_pids.get(parent.pid):
            return owner
    return None


def kill_pid(pid: int) -> None:
    """Terminate one positively identified detached page server."""
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
    with suppress(psutil.Error):
        proc.terminate()
        proc.wait(timeout=2.0)
        return
    with suppress(psutil.Error):
        proc.kill()
        proc.wait(timeout=2.0)


def _kill_page_session(backend: SessionBackend, handle: ServerHandle) -> None:
    """Stop the shell that owns the page server and every child it started."""
    backend.kill_session(handle.session_name)


def _reclaim_occupants(
    rows: list[PageRow],
    occupants: dict[int, tuple[int, str | None]],
    shell_pids: dict[int, tuple[int, str]],
    *,
    deps: ReconcileDeps | None = None,
) -> None:
    """Kill detached/orphan servers while preserving live page shells."""
    active_deps = ReconcileDeps() if deps is None else deps
    wanted_ports = {row.port for row in rows}
    own_home = str(meiva_home())
    for port, (pid, home) in occupants.items():
        if active_deps.session_owner_fn(pid, shell_pids) is not None:
            continue
        if home != own_home:
            _log.info(
                "[page-server] skipping port %s pid=%s (foreign cluster home %s)",
                port,
                pid,
                home,
            )
            continue
        if port in wanted_ports:
            _log.info("[page-server] reclaiming detached port %s pid=%s", port, pid)
        else:
            _log.info("[page-server] closing orphaned port %s pid=%s", port, pid)
        active_deps.kill_pid_fn(pid)


def _close_persisted_sessions(
    backend: SessionBackend, session_names: list[str], live_names: set[str]
) -> None:
    """Finish closed-page teardown after a daemon restart lost its cache."""
    for page_session in session_names:
        if page_session in live_names:
            _log.info("[page-server] closing persisted page session %s", page_session)
            backend.kill_session(page_session)


def _drop_stale_handles(
    backend: SessionBackend,
    wanted: dict[tuple[int, str], PageRow],
    managed: dict[tuple[int, str], ServerHandle],
    live_names: set[str],
) -> None:
    for key, handle in list(managed.items()):
        row = wanted.get(key)
        if (
            row is None
            or row.port != handle.port
            or row.serve_dir != handle.serve_dir
            or row.session_name != handle.session_name
        ):
            _kill_page_session(backend, handle)
            live_names.discard(handle.session_name)
            del managed[key]


def row_matches_snapshot(pool: ConnectionPool, row: PageRow) -> bool:
    """Whether the row this pass snapshotted is still the live supervised truth.

    ``reconcile_once`` reads open rows once and then does slow work (process
    scans, session spawns — a pass can take tens of seconds under load). A
    page can be closed or re-registered (new port / serve_dir) during that
    window; creating a session from the stale snapshot would make the next
    pass reap the fresh session as an orphan — row gone / port not wanted —
    the user-visible serve 502 race. Freshly re-read the row and require it
    to still be open and unchanged before a session is created for it.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT port, serve_dir FROM agent_pages "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NOT NULL AND host = %s",
            (row.id, row.host),
        )
        fresh = cur.fetchone()
    return fresh is not None and (fresh[0], fresh[1]) == (row.port, row.serve_dir)


@dataclass(frozen=True)
class ReconcileDeps:
    backend_factory: Callable[[], Any] = get_shell_backend
    open_rows_fn: Callable[[ConnectionPool, str], list[PageRow]] = open_rows
    live_sessions_fn: Callable[[], dict[str, Any]] = live_session_records
    occupants_fn: Callable[[], dict[int, tuple[int, str | None]]] = page_server_occupants
    shell_pids_fn: Callable[
        [dict[tuple[int, str], PageRow], dict[str, Any]], dict[int, tuple[int, str]]
    ] = page_session_shell_pids
    session_owner_fn: Callable[[int, dict[int, tuple[int, str]]], tuple[int, str] | None] = (
        page_session_owner
    )
    kill_pid_fn: Callable[[int], None] = kill_pid
    row_matches_fn: Callable[[ConnectionPool, PageRow], bool] = row_matches_snapshot
    server_healthy_fn: Callable[[str, int, str], bool] = server_is_healthy


_DEFAULT_RECONCILE_DEPS = ReconcileDeps()


def _ensure_handle(
    pool: ConnectionPool,
    backend: SessionBackend,
    row: PageRow,
    managed: dict[tuple[int, str], ServerHandle],
    now: float,
    live_names: set[str],
    bus: EventBus,
    *,
    deps: ReconcileDeps = _DEFAULT_RECONCILE_DEPS,
) -> ServerHandle | None:
    key = (row.agent_id, row.name)
    if handle := managed.get(key):
        return handle
    if not deps.row_matches_fn(pool, row):
        return None
    if not Path(row.serve_dir).is_dir():
        _log.warning("[page-server] directory unavailable for %s; explicit serve required", key)
        close_failed_page(pool, row, bus)
        return None
    if row.session_name is not None:
        if row.session_name not in live_names:
            _log.warning("[page-server] session lost for %s; automatic restore disabled", key)
            close_failed_page(pool, row, bus)
            return None
        page_session = row.session_name
        token = _ensure_token(pool, row)
    else:
        token = _ensure_token(pool, row)
        row.server_token = token
        try:
            page_session = _create_page_session(pool, backend, row, token)
        except RuntimeError:
            # meiva:note
            # A restart answers "no pty-sessions service is listening" for as long as it
            # takes the service to come up, and that is not this page's failure: the
            # claim is already rolled back, so the row is left open for the next pass
            # (task #5458). Closing it there is what made every page lose its session on
            # a restart; a service that IS there and still refused keeps today's verdict.
            if not backend.service_available():
                _log.warning(
                    "[page-server] session service not accepting for %s; admission deferred "
                    "to the next pass",
                    key,
                )
                return None
            _log.exception("[page-server] initial creation failed for %s; no retry", key)
            close_failed_page(pool, row, bus)
            return None
    handle = ServerHandle(row.agent_id, row.name, row.port, row.serve_dir, token, page_session, now)
    managed[key] = handle
    return handle


def reconcile_once(
    pool: ConnectionPool,
    managed: dict[tuple[int, str], ServerHandle],
    host: str,
    bus: EventBus,
    *,
    deps: ReconcileDeps = _DEFAULT_RECONCILE_DEPS,
) -> None:
    """Start fresh registrations once, adopt live sessions, and close failed links."""
    backend = deps.backend_factory()
    rows = deps.open_rows_fn(pool, host)
    wanted = {(row.agent_id, row.name): row for row in rows}
    live = deps.live_sessions_fn()
    live_names = set(live)
    _drop_stale_handles(backend, wanted, managed, live_names)
    _close_persisted_sessions(backend, _closed_page_sessions(pool, host), live_names)
    now = time.monotonic()
    for key, row in wanted.items():
        handle = _ensure_handle(pool, backend, row, managed, now, live_names, bus, deps=deps)
        if handle is None or now - handle.last_launch_monotonic < _SPAWN_VERIFY_TIMEOUT_S:
            continue
        if deps.server_healthy_fn(row.host, row.port, handle.token):
            continue
        _log.warning("[page-server] server unavailable for %s; automatic relaunch disabled", key)
        close_failed_page(pool, row, bus)
        _kill_page_session(backend, handle)
        del managed[key]
    live = deps.live_sessions_fn()
    occupants = deps.occupants_fn()
    _reclaim_occupants(rows, occupants, deps.shell_pids_fn(wanted, live), deps=deps)


async def reconcile_loop(
    pool: ConnectionPool,
    liveness: LoopProgress,
    config: PageServerConfig,
    bus: EventBus,
    *,
    local_machine: MachineIdentity,
) -> None:
    """Run page-shell reconciliation at the configured poll interval."""
    host = local_machine.host()
    managed: dict[tuple[int, str], ServerHandle] = {}
    _log.info(
        "[page-server] daemon started, pid=%s, machine=%s, host=%s",
        os.getpid(),
        local_machine.name(),
        host,
    )
    while True:
        liveness.beat()
        try:
            await asyncio.sleep(config.page_server_poll_interval_seconds)
            await asyncio.to_thread(reconcile_once, pool, managed, host, bus)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[page-server] schema / syntax error — code<->DB drift; retry will not self-heal",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[page-server] poll iteration failed")
            raise


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    loop_runners: tuple[
        Callable[..., Coroutine[Any, Any, None]],
        Callable[..., Coroutine[Any, Any, None]],
    ] = (reconcile_loop, dead_pages.dead_pages_loop),
) -> None:
    """Start the daemon and keep its health endpoint alive while it reconciles."""
    if resources.is_running(process_config, runtime=runtime):
        _log.info(
            "[page-server] daemon already running (pidfile=%s), exiting",
            resources.pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)
    resources.write_pidfile(process_config, runtime=runtime)
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    reconcile_progress = liveness.register("reconcile", _LIVENESS_TIMEOUT_S)
    dead_pages_progress = liveness.register("dead_show_pages", _DEAD_PAGES_LIVENESS_TIMEOUT_S)
    health = await start_health_server(
        "page_server",
        resources.endpoint(process_config, runtime=runtime).health_port,
        liveness=liveness,
        image=image,
    )
    db = database()
    pool = db.pool()
    config, bus = page_server_config(), EventBus.from_settings()
    reconcile_fn, dead_pages_fn = loop_runners
    try:
        # One TaskGroup owns the resident loops, each with its own progress tracker so
        # a stalled loop cannot be masked by a busy sibling: a loop that raises cancels
        # its sibling and ends the process, and the supervisor restarts it.
        async with asyncio.TaskGroup() as loops:
            loops.create_task(
                reconcile_fn(pool, reconcile_progress, config, bus, local_machine=local_machine)
            )
            loops.create_task(
                dead_pages_fn(pool, db, local_machine.host(), dead_pages_progress, config, bus)
            )
    finally:
        await stop_health_server(health)
        pool.close()
        resources.remove_pidfile(process_config, runtime=runtime)


@dataclass(frozen=True)
class MainDeps:
    capture_commit: Callable[[], LoadedCommit] = LoadedCommit.capture
    boot_config: Callable[[], ConfigBoot] = ConfigBoot
    check_schema: Callable[[str], None] | None = None
    version_factory: Callable[[LoadedCommit], CodeVersion] = CodeVersion
    database_cls: Callable[..., Any] = Database
    build_pipeline_fn: Callable[..., Any] = build_pipeline
    init_logging_fn: Callable[..., None] = init_gateway_process
    install_shutdown_fn: Callable[[str], None] = install_graceful_shutdown
    runner_factory: Callable[[], Any] = asyncio.Runner
    run_fn: Callable[..., Coroutine[Any, Any, None]] | None = None


_DEFAULT_MAIN_DEPS = MainDeps()


def main(
    *,
    exit_process: Callable[[int], None] = _hard_exit,
    deps: MainDeps = _DEFAULT_MAIN_DEPS,
) -> None:
    """Initialize the daemon after verifying the database schema version."""
    from base.deploy.schema.migrations import assert_schema_current

    image = deps.capture_commit()
    machine_config = deps.boot_config()
    machine_config.boot()
    machine = machine_from_boot(machine_config)
    process_home = meiva_home().resolve()
    schema_check = assert_schema_current if deps.check_schema is None else deps.check_schema
    schema_check(machine_config.view.data_plane.db_url)
    version = deps.version_factory(image)
    gate = ProcessDbGate(version=version.get, process="page_server", exempt=False)

    def database() -> Database:
        return deps.database_cls(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    pipeline = deps.build_pipeline_fn(database=database, machine=machine, home=process_home)
    deps.init_logging_fn(
        name="page_server",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    deps.install_shutdown_fn("page_server")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — an in-flight reconcile pass among them — and a stop signal
    # must never wait on those (see `_hard_exit`). The runner is therefore
    # never closed: after the explicit drain below, teardown is skipped by the
    # hard exit.
    runner = deps.runner_factory()
    active_run = run if deps.run_fn is None else deps.run_fn
    try:
        runner.run(
            active_run(
                process_config=machine_config,
                runtime=process_home / "run",
                database=database,
                image=image,
                local_machine=machine,
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[page-server] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still stops the health server,
        # closes the pool and removes the pidfile. The executor is deliberately
        # NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[page-server] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[page-server] daemon crashed — uncaught exception escaped run()")
        code = 1
    exit_process(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
