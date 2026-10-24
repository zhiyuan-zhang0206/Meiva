# meiva:note
# Per-cluster native Postgres+Redis bring-up.
#
# Every cluster (including `main`) runs its OWN Postgres and Redis under its `$MEIVA_HOME` on the
# fixed port table (`base.host.env.port_table`) — this is the only data-plane path; there is no
# shared host instance.
#
# The shell of `instance/pg_plan.py` and `instance/redis_plan.py`: those modules decide the
# authentication files, the command lines and the ordered steps of a start; this module reads the
# OS user, the platform, the machine's addresses and the binaries, executes each step and stops at
# the first one that fails. Every path is derived from the `home` the caller hands in.
#
# - Postgres `initdb`s into `$MEIVA_HOME/pg` (cold) — cached through a home-level template dir so
#   a new cluster / a test spins up by directory copy, not a fresh multi-second init — and is
#   launched directly with durable native custody on its pg port. pg_hba ALWAYS authenticates,
#   whatever the bearer: the OS-user bootstrap superuser is the sole passwordless identity (`peer`
#   on the 0700 owner-only socket — the administrator authority), and every other role is SCRAM
#   over the socket and TCP. The schema owner is NOLOGIN; application processes log in only as the
#   home's write generation (`base.cluster.authority`).
# - Redis runs `redis-server` on the cluster's redis port and ALWAYS authenticates, whatever the
#   bearer: `requirepass` = the gateway-only Redis admin password, and the cluster's ACL user has
#   its own runtime password. Both are minted at first start; a home without them is refused,
#   never converted. The admin password reaches redis through a 0600 `redis.conf` and reaches
#   `redis-cli` through `$REDISCLI_AUTH` — never argv, which `ps` shows to any local user
#   (issue #974).
#
# The Postgres db/role and Redis ACL identifiers are independent URL data: callers read each from
# its supplied URL (`db_identity(db_url)` / `redis_identity(redis_url)`) and pass `identity` /
# `redis_user` explicitly. First-start identity supplies the fixed `DATA_PLANE_IDENTITY` for both;
# later starts preserve different existing names.
#
# Bind posture — authenticated Postgres, PgBouncer and Linux Redis bind loopback and this host's
# declared reachable address (`MEIVA_MACHINE_HOST`), never all interfaces; a host that declares
# none (`localhost`) binds loopback only. macOS Redis retains its loopback-only workaround and
# the root-supervised `redis-bridge` relay (task #1469).
#
# POSIX only (macOS brew / Linux postgres + redis-server).

from __future__ import annotations

import getpass
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import psycopg

from base.cluster import ensure_cluster_redis_acl, ownership, port_preflight
from base.cluster import postgres as owned_postgres
from base.cluster.authority.monitor import MONITOR_MAP, MONITOR_ROLE
from base.cluster.dataplane.pg_admin import pg_socket_dir as _base_pg_socket_dir
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.cluster.machine import MachineIdentity
from base.host.brew_pin import brew_prefix
from base.host.net.url_secret import url_host
from base.host.private_storage import write_private_bytes
from base.host.proc import run_bounded
from base.native_process.child_env import daemon_process_env, inherited_process_env
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance import pg_plan, redis_plan
from cli.commands.data_plane.instance.bounded_wait import Wait, wait_verdict
from cli.commands.data_plane.instance.redis_binaries import (
    BrewFormula,
    OnPath,
    OverrideDir,
    redis_binary_source,
)
from cli.commands.data_plane.instance.storage_plan import require_storage_credentials
from cli.commands.data_plane.walg import warn_archive_inactive
from services.backup.walg.archive import archive_pg_args

# meiva:note
# A single cluster's own pg needs far less than a per-host shared budget. `main` on its own
# instance carries the whole prod fleet, so keep enough headroom (the gateway + long-running
# services alone hold ~25, then ~3-4 per agent).
_PG_MAX_CONNECTIONS = 500
_PG_START_TIMEOUT_S = 60.0
_PG_PROBE_TIMEOUT_S = 3.0
_INITDB_TIMEOUT_S = 300.0
_REDIS_PROBE_TIMEOUT_S = 5.0
_REDIS_LAUNCH_TIMEOUT_S = 30.0
_REDIS_READY_TIMEOUT_S = 6.0
_REDIS_READY_INTERVAL_S = 0.1

# meiva:note
# Bounded wait for this host's non-loopback bind address (MEIVA_MACHINE_HOST) to appear on a local
# interface before the Postgres data plane binds to it. On reboot brew / launchd can start `meiva`
# before the private network has assigned the address, so binding it fails and the whole
# autostart dies.
BIND_WAIT_TIMEOUT_S = 60.0
_BIND_WAIT_INTERVAL_S = 2.0

_HBA_PROOF_TIMEOUT_S = 10.0
_HBA_PROOF_INTERVAL_S = 0.1


# meiva:note
# The bounds of one start, each defaulting to its production constant: how long the reachable bind
# address may take to appear, how long a postmaster may take to answer, how long one readiness
# probe may take. A test passes small ones; nothing else sets them.
@dataclass(frozen=True)
class StartTuning:
    bind_wait_s: float = BIND_WAIT_TIMEOUT_S
    pg_start_s: float = _PG_START_TIMEOUT_S
    pg_probe_s: float = _PG_PROBE_TIMEOUT_S


PRODUCTION_TUNING = StartTuning()


def pg_data_dir(home: Path) -> Path:
    return home / "pg"


# meiva:note
# Cached `initdb` output, copied so a new instance is a directory copy rather than a
# multi-second init. Kept in the home, beside the data directory it seeds.
def pg_template_dir(home: Path) -> Path:
    return home / "pg-template-17"


# meiva:note
# This home's native Redis data directory.
def redis_data_dir(home: Path) -> Path:
    return home / "redis"


# meiva:note
# The canonical owner-only socket directory of `home`'s Postgres, created at 0700 on first use.
def pg_socket_dir(home: Path, socket_root: Path | None = None) -> Path:
    return _base_pg_socket_dir(socket_root, home=home)


# meiva:note
# One home-owned pair; an incomplete override never falls back to PATH.
def _redis_bin(redis_bin_dir: str, name: str, *, macos: bool) -> str:
    source = redis_binary_source(redis_bin_dir=redis_bin_dir, macos=macos)
    match source:
        case OverrideDir(path=root):
            for tool in ("redis-server", "redis-cli"):
                candidate = root / tool
                if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                    raise RuntimeError(
                        f"MEIVA_REDIS_BIN_DIR requires an executable file: {candidate}"
                    )
            return str(root / name)
        case BrewFormula(formula=formula):
            return str(brew_prefix(formula) / "bin" / name)
        case OnPath():
            return name
        case _:
            assert_never(source)


def redis_server_bin(redis_bin_dir: str, *, macos: bool) -> str:
    return _redis_bin(redis_bin_dir, "redis-server", macos=macos)


def redis_cli_bin(redis_bin_dir: str, *, macos: bool) -> str:
    return _redis_bin(redis_bin_dir, "redis-cli", macos=macos)


# meiva:note
# True if `addr` is currently assigned to a local interface (bindable).
#
# A Postgres or PgBouncer listener can only bind an address the kernel has on an interface;
# before the private-network interface comes up, binding its address fails with EADDRNOTAVAIL.
# Probing with a throwaway bind is the portable check.
def addr_assigned(addr: str) -> bool:
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


# meiva:note
# Block (bounded by `timeout_s`) until the host's non-loopback bind address is assigned to a
# local interface.
#
# Returns True immediately on a loopback-only (single-box) host — loopback is always present.
# Returns False on timeout so the caller fails fast with a clear message instead of the
# daemon dying on an un-bindable address.
def wait_for_reachable_bind(*, host: str, timeout_s: float) -> bool:
    if host in port_preflight.LOOPBACK_ALIASES:
        return True
    started = time.monotonic()
    while True:
        verdict = wait_verdict(
            satisfied=addr_assigned(host),
            elapsed_s=time.monotonic() - started,
            timeout_s=timeout_s,
        )
        if verdict is Wait.DONE:
            return True
        if verdict is Wait.EXPIRED:
            return False
        time.sleep(_BIND_WAIT_INTERVAL_S)


# meiva:note
# Wait for `host` to be assigned to an interface; print why and say no when it never appears.
def _await_bind(service: str, host: str, tuning: StartTuning) -> bool:
    if wait_for_reachable_bind(host=host, timeout_s=tuning.bind_wait_s):
        return True
    print(pg_plan.bind_refusal(service, host, int(tuning.bind_wait_s)), file=sys.stderr)
    return False


# meiva:note
# The machine's declared reachable address, or None on a single box that binds loopback alone.
def _off_box_host(machine: MachineIdentity) -> str | None:
    host = machine.host()
    return None if host in port_preflight.LOOPBACK_ALIASES else host


def _identity() -> pg_plan.PgIdentity:
    return pg_plan.PgIdentity(
        os_user=getpass.getuser(), monitor_role=MONITOR_ROLE, monitor_map=MONITOR_MAP
    )


# meiva:note
# Prove the RUNNING postmaster enforces password authentication.
#
# A password-less dial as a role name that cannot exist must be asked for a password over
# loopback TCP and over the owner-only socket (`pg_plan.hba_dial_verdict`). Bounded; raises
# RuntimeError naming each path that did not demand a password.
def require_authenticated_hba(pg_port: int, dial_host: str, *, socket_dir: Path) -> None:
    probe = f"meiva_hba_probe_{secrets.token_hex(4)}"
    # meiva:note
    # A path that never exists: no ~/.pgpass entry can answer the challenge.
    passfile = str(socket_dir / "no-passfile")
    started = time.monotonic()
    while True:
        unproven: list[str] = []
        for host in (dial_host, str(socket_dir)):
            error: str | None = None
            try:
                psycopg.connect(
                    host=host,
                    port=pg_port,
                    user=probe,
                    password="",
                    passfile=passfile,
                    dbname="postgres",
                    connect_timeout=3,
                ).close()
            except psycopg.OperationalError as exc:
                error = str(exc)
            reason = pg_plan.hba_dial_verdict(host, error)
            if reason is not None:
                unproven.append(reason)
        verdict = wait_verdict(
            satisfied=not unproven,
            elapsed_s=time.monotonic() - started,
            timeout_s=_HBA_PROOF_TIMEOUT_S,
        )
        if verdict is Wait.DONE:
            return
        if verdict is Wait.EXPIRED:
            raise RuntimeError(
                "postgres does not enforce password authentication after its pg_hba "
                "rewrite: " + "; ".join(unproven)
            )
        time.sleep(_HBA_PROOF_INTERVAL_S)


# meiva:note
# Fresh initdb at `target` with `os_user` as the bootstrap superuser (mirrors
# throwaway_postgres / the brew install convention).
#
# The `trust` default is never served: `start_pg` writes the always-auth hba first.
def initdb(target: Path, *, os_user: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = run_bounded(
        [
            str(pg_tool("initdb")),
            "-D",
            str(target),
            "-U",
            os_user,
            "-A",
            "trust",
            "--encoding=UTF8",
            "--locale=C",
        ],
        capture_output=True,
        text=True,
        timeout=_INITDB_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"initdb failed (rc={result.returncode}): {result.stderr.strip()}")


# meiva:note
# Ensure this cluster's pg data dir exists and is initialized, via the cached template (initdb
# once per home, then copy). Returns the data dir.
def ensure_pg_data(home: Path, *, os_user: str) -> Path:
    data = pg_data_dir(home)
    if (data / "PG_VERSION").exists():
        return data
    template = pg_template_dir(home)
    if not (template / "PG_VERSION").exists():
        initdb(template, os_user=os_user)
    shutil.copytree(template, data)
    return data


def pg_running(
    pg_isready: str, pg_port: int, host: str, *, timeout_s: float = _PG_PROBE_TIMEOUT_S
) -> bool:
    try:
        out = run_bounded(
            [pg_isready, "-h", host, "-p", str(pg_port)],
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False
    return out.returncode == 0


# meiva:note
# Execute the launch step of a Postgres start: hand the postmaster command line, built from the
# resolved binary and the declared addresses, to the native custody launcher.
def _launch_postmaster(
    config: DataPlaneConfig,
    pg_port: int,
    *,
    home: Path,
    dial_host: str,
    machine: MachineIdentity,
    retained_children: list[subprocess.Popen[bytes]],
    owner: OwnedProcess | None,
    tuning: StartTuning,
) -> None:
    data = pg_data_dir(home)
    pg_isready = str(pg_tool("pg_isready"))
    argv = pg_plan.pg_launch_argv(
        pg_plan.PgLaunch(
            postgres_bin=str(pg_tool("postgres")),
            data=data,
            port=pg_port,
            listen_addrs=tuple(port_preflight.bind_addrs(machine=machine)),
            socket_dir=pg_socket_dir(home),
            max_connections=_PG_MAX_CONNECTIONS,
            tz_args=tuple(shlex.split(pg_tz_args())),
            shm_args=tuple(shlex.split(pg_shm_args())),
            archive_args=tuple(archive_pg_args(path=config.walg_config_file)),
        )
    )
    owned_postgres.start(
        data,
        pg_port,
        argv,
        pg_start_env(),
        ready=lambda: pg_running(pg_isready, pg_port, dial_host, timeout_s=tuning.pg_probe_s),
        timeout=tuning.pg_start_s,
        expected=owner,
        retained_children=retained_children,
    )


# meiva:note
# Start (or re-affirm) this cluster's own Postgres.
#
# A foreign listener on the port refuses before anything is written or initialized; the steps
# then follow `pg_plan.plan_pg_start`. Returns 1 when the declared reachable address never
# appeared, so the caller fails fast instead of the postmaster dying on an un-bindable address.
def start_pg(
    config: DataPlaneConfig,
    pg_port: int,
    *,
    home: Path,
    retained_children: list[subprocess.Popen[bytes]],
    machine: MachineIdentity,
    tuning: StartTuning = PRODUCTION_TUNING,
) -> int:
    data = pg_data_dir(home)
    owner = ownership.require_postgres(data, pg_port, required=False)
    identity = _identity()
    dial_host = url_host(config.db_url)
    off_box = _off_box_host(machine)
    for step in pg_plan.plan_pg_start(owner_retained=owner is not None, off_box_host=off_box):
        match step:
            case pg_plan.EnsureData():
                ensure_pg_data(home, os_user=identity.os_user)
            case pg_plan.WriteAuthentication():
                (data / "pg_ident.conf").write_text(pg_plan.pg_ident_body(identity))
                hba = pg_plan.pg_hba_body(
                    identity, reachable_host=off_box, trusted_cidrs=config.trusted_cidrs
                )
                (data / "pg_hba.conf").write_text(hba)
            case pg_plan.AwaitReachableBind(host=host):
                if not _await_bind("postgres", host, tuning):
                    return 1
            case pg_plan.LaunchPostmaster():
                _launch_postmaster(
                    config,
                    pg_port,
                    home=home,
                    dial_host=dial_host,
                    machine=machine,
                    retained_children=retained_children,
                    owner=owner,
                    tuning=tuning,
                )
            case pg_plan.ProveAuthentication():
                ownership.require_postgres(data, pg_port)
                require_authenticated_hba(pg_port, dial_host, socket_dir=pg_socket_dir(home))
                print(
                    f"  ✓ postgres ready ({dial_host}:{pg_port}, password authentication enforced)"
                )
            case pg_plan.CheckArchive():
                warn_archive_inactive(config.walg_config_file)
            case _:
                assert_never(step)
    return 0


# meiva:note
# The child env that authenticates `redis-cli` without a secret on its command line.
#
# Raises ValueError when the admin password is empty: the owned Redis always requires it.
def redis_cli_env(redis_admin_password: str) -> dict[str, str]:
    return inherited_process_env(redis_plan.redis_auth_env(redis_admin_password))


def redis_running(redis_cli: str, redis_admin_password: str, redis_port: int, host: str) -> bool:
    try:
        out = run_bounded(
            [redis_cli, "-h", host, "-p", str(redis_port), "ping"],
            env=redis_cli_env(redis_admin_password),
            capture_output=True,
            text=True,
            timeout=_REDIS_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return "PONG" in (out.stdout or "")


# meiva:note
# Write the cluster's RDB save schedule and `requirepass` into a 0600 `redis.conf` and return it.
#
# `redis-server --requirepass <password>` would carry the Redis admin password on argv. redis
# overwrites its own process title moments later, so `ps` only sees it during startup — but a
# startup window is still a window, and the conf file is the mechanism redis itself documents.
# Every render carries `requirepass`: Redis always authenticates.
def write_redis_conf(data: Path, redis_admin_password: str) -> Path:
    content = redis_plan.redis_conf_text(redis_admin_password)
    conf = data / "redis.conf"
    write_private_bytes(conf, content.encode())
    return conf


# meiva:note
# One Redis start in progress: what its steps read, and the executor of each step.
#
# A step returns 0 or the exit code the start stops with.
@dataclass(frozen=True)
class _RedisStart:
    config: DataPlaneConfig
    redis_port: int
    runtime_password: str
    identity: str
    home: Path
    machine: MachineIdentity
    tuning: StartTuning
    macos: bool
    redis_cli: str
    dial_host: str

    @property
    def data(self) -> Path:
        return redis_data_dir(self.home)

    def execute(self, step: redis_plan.RedisStep) -> int:
        match step:
            case redis_plan.AffirmAcl():
                return ensure_redis_acl(
                    self.config,
                    self.redis_port,
                    self.runtime_password,
                    self.identity,
                    self.dial_host,
                    redis_data=self.data,
                )
            case redis_plan.WriteConf():
                write_redis_conf(self.data, self.config.redis_admin_password)
            case redis_plan.RequireFreePort():
                port_preflight.require_listener(None, self.redis_port, required=False)
            case redis_plan.AwaitReachableBind(host=host):
                return 0 if _await_bind("redis", host, self.tuning) else 1
            case redis_plan.PrepareDataDir():
                self.data.mkdir(parents=True, exist_ok=True)
            case redis_plan.LaunchServer():
                launched = _launch_redis(
                    self.config, self.redis_port, self.data, self.machine, macos=self.macos
                )
                return 0 if launched else 1
            case redis_plan.AwaitServerReady():
                return self._await_ready()
            case redis_plan.ReportReady(already_running=already):
                state = "already running" if already else "started"
                print(f"  ✓ redis {state} ({self.dial_host}:{self.redis_port})")
            case _:
                assert_never(step)
        return 0

    def _await_ready(self) -> int:
        admin = self.config.redis_admin_password
        if _await_redis(self.redis_cli, admin, self.redis_port, self.dial_host):
            return 0
        print(f"  ✗ redis did not become ready on :{self.redis_port}", file=sys.stderr)
        return 1


# meiva:note
# Start (or re-affirm) this cluster's own Redis, always authenticated.
#
# Raises ValueError, before any effect, when the admin or runtime password is missing. The
# steps follow `redis_plan.plan_redis_start`; a foreign listener on the port refuses the start.
def start_redis(
    config: DataPlaneConfig,
    redis_port: int,
    runtime_password: str,
    identity: str,
    *,
    home: Path,
    machine: MachineIdentity,
    macos: bool,
    tuning: StartTuning = PRODUCTION_TUNING,
) -> int:
    redis_plan.require_redis_credentials(config.redis_admin_password, runtime_password)
    redis_cli = redis_cli_bin(config.redis_bin_dir, macos=macos)
    dial_host = url_host(config.redis_url)
    admin = config.redis_admin_password
    start = _RedisStart(
        config=config,
        redis_port=redis_port,
        runtime_password=runtime_password,
        identity=identity,
        home=home,
        machine=machine,
        tuning=tuning,
        macos=macos,
        redis_cli=redis_cli,
        dial_host=dial_host,
    )
    running = redis_running(redis_cli, admin, redis_port, dial_host)
    steps = redis_plan.plan_redis_start(
        running=running, macos=macos, off_box_host=_off_box_host(machine)
    )
    for step in steps:
        if (rc := start.execute(step)) != 0:
            return rc
    return 0


# meiva:note
# The `redis-server --daemonize yes` command line of this cluster's Redis.
#
# The platform decides two things: which `redis-server` is run (`redis_server_bin`) and which
# addresses it binds (`redis_plan.redis_bind_addrs`: loopback alone on macOS, else the machine's
# declared bind addresses).
def redis_launch_argv(
    config: DataPlaneConfig, redis_port: int, data: Path, machine: MachineIdentity, *, macos: bool
) -> list[str]:
    bind = redis_plan.redis_bind_addrs(
        macos=macos, declared=tuple(port_preflight.bind_addrs(machine=machine))
    )
    return redis_plan.redis_server_argv(
        redis_plan.RedisLaunch(
            server_bin=redis_server_bin(config.redis_bin_dir, macos=macos),
            conf=data / "redis.conf",
            port=redis_port,
            bind_addrs=bind,
            data=data,
        )
    )


# meiva:note
# Run `redis-server --daemonize yes` on the planned command line; print why and say no when it
# exits non-zero.
def _launch_redis(
    config: DataPlaneConfig, redis_port: int, data: Path, machine: MachineIdentity, *, macos: bool
) -> bool:
    result = run_bounded(
        redis_launch_argv(config, redis_port, data, machine, macos=macos),
        capture_output=True,
        text=True,
        env=daemon_process_env(),
        timeout=_REDIS_LAUNCH_TIMEOUT_S,
    )
    if result.returncode != 0:
        print(
            f"  ✗ redis-server failed (rc={result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


# meiva:note
# `redis-server --daemonize` returns at once; poll until the port answers PING.
def _await_redis(redis_cli: str, admin: str, redis_port: int, dial_host: str) -> bool:
    started = time.monotonic()
    while True:
        verdict = wait_verdict(
            satisfied=redis_running(redis_cli, admin, redis_port, dial_host),
            elapsed_s=time.monotonic() - started,
            timeout_s=_REDIS_READY_TIMEOUT_S,
        )
        if verdict is Wait.DONE:
            return True
        if verdict is Wait.EXPIRED:
            return False
        time.sleep(_REDIS_READY_INTERVAL_S)


# meiva:note
# Add the cluster's ACL user (the runtime identity, names-as-data — passed in by the caller,
# never derived from a name) on top of `requirepass`.
#
# requirepass authenticates the `default` admin user, which provisions the ACL user. Re-affirmed
# every start (survives a restart that drops the in-memory ACL). Both passwords are always
# present: Redis never runs without auth. The runtime URLs carry the identity as username, and
# redis-py AUTHes as that user, so a missing user would WRONGPASS forever and the redis wake bus
# would never deliver to agents. Returns 1, after printing why, when the provisioning refuses.
def ensure_redis_acl(
    config: DataPlaneConfig,
    redis_port: int,
    runtime_password: str,
    identity: str,
    redis_host: str,
    *,
    redis_data: Path,
) -> int:
    admin = f"redis://default:{config.redis_admin_password}@{redis_host}:{redis_port}"
    try:
        ensure_cluster_redis_acl(
            identity,
            redis_admin_url=admin,
            runtime_password=runtime_password,
            channel_prefix=config.events_channel.removesuffix(":events"),
            expected_data_dir=redis_data,
        )
    except Exception as exc:
        print(f"  ✗ ensuring cluster redis user failed: {exc}", file=sys.stderr)
        return 1
    return 0


# meiva:note
# Ensure owned Postgres and Redis; schema, authority and pooler follow in start order
# (`cli.commands.data_plane.bringup`).
#
# Raises ValueError, before any native effect, when a required credential is missing: Redis
# always needs its admin and runtime passwords. Postgres needs none: its administrator is the
# OS user over the owner-only socket.
def ensure_cluster_storage(
    config: DataPlaneConfig,
    *,
    home: Path,
    pg_port: int,
    redis_port: int,
    redis_password: str,
    redis_user: str,
    retained_children: list[subprocess.Popen[bytes]],
    machine: MachineIdentity,
    macos: bool,
) -> int:
    require_storage_credentials(config.redis_admin_password, redis_password)
    print(f"\n→ per-cluster data plane (pg :{pg_port}, redis :{redis_port})")
    rc = start_pg(config, pg_port, home=home, retained_children=retained_children, machine=machine)
    if rc != 0:
        return rc
    return start_redis(
        config, redis_port, redis_password, redis_user, home=home, machine=machine, macos=macos
    )


# meiva:note
# True if this cluster's Redis answers PING as its `default` user (using the gateway-only Redis
# admin password). Used by `meiva status`; degrades to False on any error, including a home
# without its admin password.
def redis_reachable(redis_admin_password: str, redis_port: int, redis_host: str) -> bool:
    import redis as _redis
    from redis.exceptions import RedisError

    client = _redis.Redis(
        host=redis_host,
        port=redis_port,
        password=redis_admin_password,
        socket_connect_timeout=3,
    )
    try:
        return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]
    except (RedisError, OSError):
        return False
    finally:
        client.close()
