# meiva:note
# Per-cluster PgBouncer transaction pooler: the shell that observes, runs what
# `pooler/convergence.py` plans, and stops it.
#
# The pooled front door consumers dial past about 50 agents (each agent holds 2 Postgres
# connections). PgBouncer is the third per-cluster data-plane process, a peer of this
# cluster's own Postgres and Redis (`cluster_instance.py`), brought up on the cluster's own
# `pgbouncer` port (a registry-record fact; MEIVA_DB_URL carries it) after schema preparation
# and group grants whenever MEIVA_PGBOUNCER_ENABLED (on by default: past about 50 agents
# pooling is the density path). Setting it false is a kill-switch: nothing here runs, converge
# rewrites MEIVA_DB_URL to the direct Postgres port, and every consumer talks to Postgres
# directly through the one URL.
#
# Auth is always authenticated, whatever the cluster secret
# (docs/decisions/2026-09-26-internal-data-plane-always-authenticated.md):
#
# - **client to pgbouncer**: `auth_type = scram-sha-256` against a `userlist.txt` (0600)
#   rendered by `base.host.db_authority.delivery.render_userlist`: exactly the delivered write
#   generation's two logins with their stored SCRAM verifiers, plus the operator admin-console
#   entry `meiva_pooler_admin` (a userlist-only name, never a PostgreSQL role). No plaintext
#   password, no owner entry.
# - **pgbouncer to postgres**: over the owner-only unix socket with SCRAM pass-through. The
#   userlist verifier equals `pg_authid.rolpassword`, so the client's SCRAM keys authenticate
#   the backend hop (`local all all scram-sha-256`); the pooler holds no server credential.
# - **userlist change means restart**: a SIGHUP reload keeps a removed user that already
#   authenticated and still admits its new sessions, so changed userlist (or ini) bytes restart
#   the pooler; unchanged bytes only reload.
#
# `pool_mode = transaction`: agent and daemon client pools collapse onto a small set of real
# Postgres backends. Every pooled consumer connects with `prepare_threshold=None` (no
# server-side prepared statements; psycopg3's `0` would mean prepare on the FIRST execution, and
# two fresh connections preparing the same `_pg3_0` name on one backend raise
# DuplicatePreparedStatement), which is what makes transaction pooling safe across the
# different backends a transaction hands out.
#
# POSIX only (macOS brew or Linux apt `pgbouncer`).
# meiva:link cli/commands/data_plane/docs/data-plane-startup.meiva.okf.md

from __future__ import annotations

import shutil
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, assert_never

import psutil

from base.cluster import ownership, port_preflight
from base.cluster.dataplane import pooler as pooler_files
from base.cluster.machine import MachineIdentityReader
from base.host.db_authority.delivery import POOLER_ADMIN
from base.host.private_storage import write_private_bytes
from base.host.proc import process_alive, run_bounded
from base.native_process.os_platform import LockTimeoutError
from base.native_process.ownership import OwnedProcess
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.cluster_instance import wait_for_reachable_bind
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot
from cli.commands.data_plane.pooler.convergence import (
    AnnounceChangedRestart,
    AnnounceDegradedRestart,
    AwaitReachableBind,
    Launch,
    PoolerObservation,
    PoolerStep,
    Reload,
    StopRunning,
    WriteFiles,
    plan_convergence,
)
from cli.commands.data_plane.pooler.identity import anchored_ini, ini_argument
from cli.commands.data_plane.pooler.ini import IniInputs
from cli.commands.data_plane.pooler.ini import render_ini as render_ini_text
from cli.commands.data_plane.pooler.text import (
    PoolerText,
    install_command,
    not_installed_error,
    not_installed_warning,
    stale_pidfile,
)

# meiva:note
# How long a restart gives the running pooler's safe shutdown, and a stop-only caller's.
RESTART_STOP_WAIT_S = 5.0

# meiva:note
# How long a freshly launched pooler has to answer on its admin console.
READY_WAIT_S = 6.0

# meiva:note
# The pause between two admin-console probes of a pooler that is still starting.
_READY_POLL_S = 0.1

# meiva:note
# The bound on `pgbouncer -d` itself: it daemonizes and returns at once.
_LAUNCH_TIMEOUT_S = 30.0


# meiva:note
# What the rendered ini depends on. `socket_dir` is the directory the running Postgres
# listens on (the one the admin dial uses), never a recomputed canonical one.
@dataclass(frozen=True)
class PoolerShape:
    pg_port: int
    listen_port: int
    db_name: str
    machine: MachineIdentityReader
    max_client_conn: int
    socket_dir: Path


# meiva:note
# One bring-up of the pooler.
#
# `userlist` is the exact `auth_file` and `admin_password` the admin-console credential it
# carries. `binary` is the resolved pgbouncer path and `daemon_env` the environment the daemon
# is born with (only process mechanics, never configuration or a credential: the boot pass may
# have put the gateway login, the write generation and its API token into the launching process,
# and a daemon would keep them until it restarts). `macos` is the host platform, which words the
# install hint of a pooler that is not installed. The waits are tuning values: how long the
# reachable address may take to appear, the running pooler's safe shutdown, and the new
# pooler's first answer.
@dataclass(frozen=True)
class PoolerSpec:
    shape: PoolerShape
    userlist: bytes = field(repr=False)
    admin_password: str = field(repr=False)
    binary: str
    daemon_env: Mapping[str, str] = field(repr=False)
    macos: bool
    bind_wait_s: float
    stop_wait_s: float = RESTART_STOP_WAIT_S
    ready_wait_s: float = READY_WAIT_S


def userlist_path() -> Path:
    return pooler_files.pooler_dir() / "userlist.txt"


def _logfile_path() -> Path:
    return pooler_files.pooler_dir() / "pgbouncer.log"


# meiva:note
# The pgbouncer.ini for this cluster's pooler: the shape's values plus the facts this host
# gives (the declared reachable address adds one bind, the default `localhost` leaves loopback
# alone; the file paths of this home). The statement-ceiling statement is the same constant as
# the client-side SET, imported lazily because this runs in data-plane bring-up, before any
# settings or env load is guaranteed.
def render_ini(shape: PoolerShape) -> str:
    from base.db import PG_STATEMENT_TIMEOUT_SET_SQL

    return render_ini_text(
        IniInputs(
            db_name=shape.db_name,
            pg_port=shape.pg_port,
            socket_dir=str(shape.socket_dir),
            listen_addrs=tuple(port_preflight.bind_addrs(machine=shape.machine)),
            listen_port=shape.listen_port,
            userlist_path=str(userlist_path()),
            logfile=str(_logfile_path()),
            pidfile=str(pooler_files.pidfile_path()),
            max_client_conn=shape.max_client_conn,
            admin_user=POOLER_ADMIN,
            statement_timeout_sql=PG_STATEMENT_TIMEOUT_SET_SQL,
        )
    )


def _desired_files(spec: PoolerSpec) -> tuple[tuple[Path, bytes], ...]:
    return (
        (pooler_files.ini_path(), render_ini(spec.shape).encode()),
        (userlist_path(), spec.userlist),
    )


def _stale_files(spec: PoolerSpec) -> tuple[tuple[Path, bytes], ...]:
    stale: list[tuple[Path, bytes]] = []
    for path, body in _desired_files(spec):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != body:
            stale.append((path, body))
    return tuple(stale)


# meiva:note
# Write pgbouncer.ini and userlist.txt (0600) where their bytes changed; True when either
# did, which requires a pooler restart (a reload never revokes a user).
def write_config(spec: PoolerSpec) -> bool:
    stale = _stale_files(spec)
    for path, body in stale:
        write_private_bytes(path, body)
    return bool(stale)


# meiva:note
# The resolved path of the ini a pgbouncer process was started with: "" when its command line
# names none (it cannot be this home's, which always starts with `-d <ini>`), None when the
# process cannot be introspected. An unreadable working directory only matters when the
# argument is relative.
def config_of(process: psutil.Process) -> str | None:
    try:
        argument = ini_argument(process.cmdline())
        if argument is None:
            return ""
        cwd = process.cwd() if not argument.startswith("/") else "/"
        return str(Path(anchored_ini(argument, cwd)).resolve())
    except (psutil.AccessDenied, OSError):
        return None


# meiva:note
# Whether `pid` is really THIS home's pooler, and not a stranger that inherited the number.
#
# A stale pidfile can name a recycled process after a crash or explicit force stop. Validate
# its executable and exact configuration path before treating it as a candidate; native custody
# also captures its process birth before any reload or shutdown signal. A process this user
# cannot introspect counts as NOT ours: a pooler that outlives its stop retains custody.
def pid_is_our_pooler(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        if process.name() != "pgbouncer":
            return False
        return config_of(process) == str(pooler_files.ini_path().resolve())
    except (psutil.Error, OSError):
        return False


# meiva:note
# The pid of a live pgbouncer OWNED BY THIS HOME (from its pidfile), or None.
#
# Stop uses this for stale-pidfile cleanup before capturing native custody. A pidfile that no
# longer names our pooler, because the process is gone or the number was since recycled onto
# someone else, reads as None and is removed, so the next bring-up starts from a clean slate
# instead of re-deciding against the same dead number. A live stranger is reported.
def running_pid() -> int | None:
    pidfile = pooler_files.pidfile_path()
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    if pid_is_our_pooler(pid):
        return pid
    if process_alive(pid):
        print(stale_pidfile(str(pidfile), pid), file=sys.stderr)
    pidfile.unlink(missing_ok=True)
    return None


def _accepting_pooler(listen_port: int) -> OwnedPooler | None:
    owner = ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path())
    if owner is None:
        port_preflight.require_listener(None, listen_port, required=False)
        return None
    custodian = OwnedPooler(owner, listen_port, pooler_files.ini_path())
    custodian.require_accepting()
    return custodian


def binary_installed(binary: str) -> bool:
    return Path(binary).exists() or shutil.which(binary) is not None


# meiva:note
# How to install pgbouncer on this platform. The provisioning script is located through the
# process's checkout (`repo_root()`), the one read here that still comes from the process.
def _install_hint(*, macos: bool) -> str:
    from base.paths import repo_root

    return install_command(
        macos=macos, provision_script=str(repo_root() / "scripts/provision/database.sh")
    )


@dataclass(frozen=True)
class _Run:
    spec: PoolerSpec
    text: PoolerText
    custodian: OwnedPooler | None

    def running(self) -> OwnedPooler:
        if self.custodian is None:
            raise RuntimeError("the pooler plan acts on a running pooler that was not observed")
        return self.custodian


# meiva:note
# SIGHUP the pooler birth that was observed. A birth that is gone or replaced since is never
# signalled: the pid may name a stranger now.
def reload_pooler(owner: OwnedProcess) -> None:
    if not owner.send_signal(signal.SIGHUP):
        raise RuntimeError("PgBouncer identity changed before reload")


def _reload(run: _Run) -> int:
    reload_pooler(run.running().identity)
    if not pooler_files.admin_reachable(run.spec.shape.listen_port, run.spec.admin_password):
        print(run.text.admin_refused(), file=sys.stderr)
        return 1
    print(run.text.reloaded())
    return 0


def _launch(run: _Run) -> int:
    spec, text = run.spec, run.text
    result = run_bounded(
        [spec.binary, "-d", str(pooler_files.ini_path())],
        timeout=_LAUNCH_TIMEOUT_S,
        capture_output=True,
        text=True,
        env=dict(spec.daemon_env),
    )
    if result.returncode != 0:
        print(text.launch_failed(result.returncode, result.stderr), file=sys.stderr)
        return 1
    # meiva:note
    # `-d` daemonizes and returns at once; wait for the admin console to authenticate. The
    # caller proves each delivered login's pooled backend.
    deadline = time.monotonic() + spec.ready_wait_s
    while not pooler_files.admin_reachable(spec.shape.listen_port, spec.admin_password):
        left = deadline - time.monotonic()
        if left <= 0:
            print(text.not_ready(), file=sys.stderr)
            return 1
        time.sleep(min(_READY_POLL_S, left))
    machine = spec.shape.machine
    if not pooler_files.pgbouncer_public_listener_reachable(
        spec.shape.listen_port, POOLER_ADMIN, machine=machine
    ):
        # meiva:note
        # The pooler is up on loopback but NOT on the address consumers dial: the silent
        # degradation this bring-up exists to never ship. Loud failure; the boot retry and the
        # watchdog keep re-running, and once the private network is up the next pass restarts
        # the pooler into a healthy double bind.
        print(text.degraded_after_launch(), file=sys.stderr)
        return 1
    port_preflight.require_listener(
        ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path()),
        spec.shape.listen_port,
    )
    print(text.started())
    return 0


def _perform(step: PoolerStep, run: _Run) -> int:
    spec, text = run.spec, run.text
    match step:
        case WriteFiles():
            write_config(spec)
        case Reload():
            return _reload(run)
        case AwaitReachableBind(context):
            if not wait_for_reachable_bind(host=text.host, timeout_s=spec.bind_wait_s):
                print(text.bind_missing(context), file=sys.stderr)
                return 1
        case AnnounceChangedRestart():
            print(text.changed_restart())
        case AnnounceDegradedRestart():
            print(text.degraded_restart(), file=sys.stderr)
        case StopRunning():
            custodian = run.running()
            if not custodian.stop(deadline=time.monotonic() + spec.stop_wait_s):
                print(text.stop_survived(custodian.identity.pid), file=sys.stderr)
                return 1
        case Launch():
            return _launch(run)
        case _:
            assert_never(step)
    return 0


# meiva:note
# Bring up (or reload) this cluster's PgBouncer on the shape's `listen_port`, pooling in front
# of the local Postgres. Idempotent. Returns 0 on success.
#
# What happens to a running pooler is decided by `plan_convergence`. A restart uses the safe
# shutdown only: a pooler that will not stop (a client holding a transaction) keeps custody and
# fails this call rather than being killed underneath an ordinary start. After any (re)start the
# pooler must prove it listens on the reachable address too; the wait for that address guards
# only the paths that (re)start a pooler.
#
# A pooler that is not installed fails fast and is never silently replaced by a direct
# connection: the operator asked for pooling. A listener the pooler's custody cannot account for
# refuses before any file is written or signal sent.
#
# Only called when MEIVA_PGBOUNCER_ENABLED (gated by the caller in
# `complete_gateway_data_plane`).
def ensure_pgbouncer(spec: PoolerSpec) -> int:
    shape = spec.shape
    if not binary_installed(spec.binary):
        print(not_installed_error(spec.binary, _install_hint(macos=spec.macos)), file=sys.stderr)
        return 1
    custodian = _accepting_pooler(shape.listen_port)
    public = custodian is not None and pooler_files.pgbouncer_public_listener_reachable(
        shape.listen_port, POOLER_ADMIN, machine=shape.machine
    )
    observation = PoolerObservation(
        running=custodian is not None,
        files_changed=bool(_stale_files(spec)),
        public_listener=public,
    )
    text = PoolerText(
        host=shape.machine.host(),
        bind_wait_s=spec.bind_wait_s,
        listen_port=shape.listen_port,
        logfile=str(_logfile_path()),
    )
    run = _Run(spec, text, custodian)
    for step in plan_convergence(observation):
        if failure := _perform(step, run):
            return failure
    return 0


PoolerStop = Literal["not-running", "stopped", "forced"]


# meiva:note
# Stop this home's captured pooler; a previous drain is only waited on.
#
# `force` escalates a pooler whose safe shutdown did not finish within its bound to SIGKILL;
# only a caller that owns an already-quiescent, already-revoked data plane passes it. Returns
# how the stop ended: no pooler of this home was running, the safe shutdown completed, or force
# escalated.
def stop_pgbouncer(*, force: bool = False) -> PoolerStop:
    pid = running_pid()
    if pid is None:
        return "not-running"
    owner = ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path())
    if owner is None:
        return "not-running"
    custodian = OwnedPooler.from_config(owner, pooler_files.ini_path())
    deadline = time.monotonic() + RESTART_STOP_WAIT_S
    try:
        if custodian.stop(deadline=deadline):
            return "stopped"
    except (TimeoutError, LockTimeoutError):
        if not force:
            raise
    # meiva:note
    # The safe shutdown's bound is spent, so a forced stop goes straight to its own settle
    # wait; the recorded stop intent is never signalled twice.
    if force and custodian.stop(deadline=deadline, force=True):
        return "forced"
    raise RuntimeError("PgBouncer stop incomplete; custody retained")


# meiva:note
# `current` rewritten onto the toggle-matched port, or None when it needs no change.
#
# Only a URL carrying the OTHER port of this cluster is rewritten; a placeholder, an empty
# value or an operator stand-in naming neither port is left alone.
def _normalized_db_url(
    current: str, *, pg_port: int, pooler_port: int, pooler_enabled: bool
) -> str | None:
    from urllib.parse import urlsplit

    from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
    from base.host.net.url_secret import url_with_port

    if not current or current == PLACEHOLDER_DB_URL:
        return None
    try:
        port = urlsplit(current).port
    except ValueError:
        return None
    if port is None:
        return None
    want = pooler_port if pooler_enabled else pg_port
    if port != want and port in (pg_port, pooler_port):
        return url_with_port(current, want)
    return None


def ensure_pgbouncer_step(ctx: ConvergeCtx) -> None:
    # meiva:note
    # MEIVA_DB_URL's port follows MEIVA_PGBOUNCER_ENABLED at URL generation, so this step
    # rewrites only a URL carrying this cluster's other port; a stand-in URL naming neither
    # port is left untouched, and a fresh birth already carries the toggle-matched port.
    #
    # A missing pooler binary is reported here and refused by data-plane bring-up; the
    # start-wide host dependency preflight installs it before converge.
    # meiva:link cli/commands/data_plane/docs/data-plane-startup.meiva.okf.md
    missing = converge_pgbouncer(data_plane_config_from_boot(ctx.read_config()), ctx.meiva_home)
    if missing is not None:
        hint = _install_hint(macos=ctx.host.platform.system == "darwin")
        print(not_installed_warning(missing, hint), file=sys.stderr)


# meiva:note
# The converge step's work for `home` under `config` (see `ensure_pgbouncer_step`): the port
# rewrite, and the pooler binary the step should warn about, or None when there is nothing to
# warn about (no pooler, a remote plane, or the binary is installed).
def converge_pgbouncer(config: DataPlaneConfig, home: Path) -> str | None:
    from dotenv import dotenv_values

    from base.cluster import get_record
    from base.host.env.dotenv_file import upsert_env
    from ops.env_audit import EnvWriteAudit

    if config.is_remote:
        # meiva:note
        # The pooler is a local-instance component; a remote/SaaS plane's URL is the
        # provider's (its port is not this cluster's), so converge must neither rewrite it nor
        # preflight the local binary.
        return None
    rec = get_record(home)
    if rec is None:
        return None
    env_path = home / ".env"
    current = (dotenv_values(env_path).get("MEIVA_DB_URL") or "").strip()
    normalized = _normalized_db_url(
        current,
        pg_port=rec.ports["postgres"],
        pooler_port=rec.ports["pgbouncer"],
        pooler_enabled=config.pgbouncer_enabled,
    )
    if normalized:
        upsert_env(
            env_path, {"MEIVA_DB_URL": normalized}, audit=EnvWriteAudit(site="converge_pgbouncer")
        )
    if not config.pgbouncer_enabled:
        return None
    binary = pooler_files.pgbouncer_bin()
    return None if binary_installed(binary) else binary
