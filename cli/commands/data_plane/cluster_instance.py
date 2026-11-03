# meiva:note
# Per-cluster native Postgres bring-up, and the ordered start of the pair it belongs to.
#
# Every cluster (including `main`) runs its OWN Postgres and Redis under its `$MEIVA_HOME` on the
# fixed port table (`base.host.env.port_table`) — this is the only data-plane path; there is no
# shared host instance.
#
# The shell of `instance/pg_plan.py`: that module decides the authentication files, the command
# lines and the ordered steps of a start; this module reads the OS user, the platform, the
# machine's addresses and the binaries, executes each step and stops at the first one that fails.
# Every path is derived from the `home` the caller hands in. The Redis half of the same pair is
# `redis_instance.py`'s shell of `instance/redis_plan.py`, and the bounds both halves start under
# (`StartTuning`, the reachable-address wait) are `start_bounds.py`.
#
# - Postgres `initdb`s into `$MEIVA_HOME/pg` (cold) — cached through a home-level template dir so
#   a new cluster / a test spins up by directory copy, not a fresh multi-second init — and is
#   launched directly with durable native custody on its pg port. pg_hba ALWAYS authenticates,
#   whatever the bearer: the OS-user bootstrap superuser is the sole passwordless identity (`peer`
#   on the 0700 owner-only socket — the administrator authority), and every other role is SCRAM
#   over the socket and TCP. The schema owner is NOLOGIN; application processes log in only as the
#   home's write generation (`base.cluster.authority`).
# - Redis (`redis_instance.start_redis`) runs `redis-server` on the cluster's redis port and always
#   authenticates; `ensure_cluster_storage` starts it only after the postmaster answers.
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
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

import psycopg

from base.clock import Timers
from base.cluster import ownership, port_preflight
from base.cluster import postgres as owned_postgres
from base.cluster.authority.monitor import MONITOR_MAP, MONITOR_ROLE
from base.cluster.dataplane.pg_admin import pg_socket_dir as _base_pg_socket_dir
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.cluster.machine import MachineIdentityReader
from base.host.net.url_secret import url_host
from base.host.system.hostport import Host
from base.native_process.ownership import OwnedProcess
from cli.boundary.process import stderr_text
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance import pg_plan
from cli.commands.data_plane.instance.bounded_wait import Wait, wait_verdict
from cli.commands.data_plane.instance.storage_plan import require_storage_credentials
from cli.commands.data_plane.native.redis_instance import start_redis
from cli.commands.data_plane.native.start_bounds import (
    PRODUCTION_TUNING,
    StartTuning,
    await_bind,
    off_box_host,
)
from cli.commands.data_plane.pg_probe import PostmasterWait, launch_failure
from cli.commands.data_plane.walg import warn_archive_inactive
from services.backup.walg.archive import archive_pg_args

# meiva:note
# A single cluster's own pg needs far less than a per-host shared budget. `main` on its own
# instance carries the whole prod fleet, so keep enough headroom (the gateway + long-running
# services alone hold ~25, then ~3-4 per agent).
_PG_MAX_CONNECTIONS = 500
_INITDB_TIMEOUT_S = 300.0


_HBA_PROOF_TIMEOUT_S = 10.0
_HBA_PROOF_INTERVAL_S = 0.1


def pg_data_dir(home: Path) -> Path:
    return home / "pg"


# meiva:note
# Cached `initdb` output, copied so a new instance is a directory copy rather than a
# multi-second init. Kept in the home, beside the data directory it seeds.
def pg_template_dir(home: Path) -> Path:
    return home / "pg-template-17"


# meiva:note
# The canonical owner-only socket directory of `home`'s Postgres, created at 0700 on first use.
def pg_socket_dir(home: Path, socket_root: Path | None = None) -> Path:
    return _base_pg_socket_dir(socket_root, home=home)


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
def require_authenticated_hba(
    pg_port: int, dial_host: str, *, socket_dir: Path, timers: Timers
) -> None:
    probe = f"meiva_hba_probe_{pg_port}"
    # meiva:note
    # A path that never exists: no ~/.pgpass entry can answer the challenge.
    passfile = str(socket_dir / "no-passfile")
    started = timers.monotonic()
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
            elapsed_s=timers.monotonic() - started,
            timeout_s=_HBA_PROOF_TIMEOUT_S,
        )
        if verdict is Wait.DONE:
            return
        if verdict is Wait.EXPIRED:
            raise RuntimeError(
                "postgres does not enforce password authentication after its pg_hba "
                "rewrite: " + "; ".join(unproven)
            )
        timers.sleep(_HBA_PROOF_INTERVAL_S)


# meiva:note
# Fresh initdb at `target` with `os_user` as the bootstrap superuser (mirrors
# throwaway_postgres / the brew install convention).
#
# The `trust` default is never served: `start_pg` writes the always-auth hba first. The program
# runs through the host the caller hands in, like every other process this module starts.
def initdb(target: Path, *, os_user: str, home: Path, host: Host) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = host.run(
        [
            str(pg_tool("initdb", home=home)),
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
        raise RuntimeError(f"initdb failed (rc={result.returncode}): {stderr_text(result).strip()}")


# meiva:note
# Ensure this cluster's pg data dir exists and is initialized, via the cached template (initdb
# once per home, then copy). Returns the data dir.
def ensure_pg_data(home: Path, *, os_user: str, host: Host) -> Path:
    data = pg_data_dir(home)
    if (data / "PG_VERSION").exists():
        return data
    template = pg_template_dir(home)
    if not (template / "PG_VERSION").exists():
        initdb(template, os_user=os_user, home=home, host=host)
    shutil.copytree(template, data)
    return data


# meiva:note
# Execute the launch step of a Postgres start: hand the postmaster command line, built from the
# resolved binary and the declared addresses, to the native custody launcher.
def _launch_postmaster(
    config: DataPlaneConfig,
    pg_port: int,
    *,
    home: Path,
    host: Host,
    dial_host: str,
    machine: MachineIdentityReader,
    retained_children: list[subprocess.Popen[bytes]],
    owner: OwnedProcess | None,
    tuning: StartTuning,
    timers: Timers,
    system: str,
    environ: Mapping[str, str],
) -> None:
    data = pg_data_dir(home)
    pg_isready = str(pg_tool("pg_isready", home=home))
    argv = pg_plan.pg_launch_argv(
        pg_plan.PgLaunch(
            postgres_bin=str(pg_tool("postgres", home=home)),
            data=data,
            port=pg_port,
            listen_addrs=tuple(port_preflight.bind_addrs(machine=machine)),
            socket_dir=pg_socket_dir(home),
            max_connections=_PG_MAX_CONNECTIONS,
            tz_args=tuple(shlex.split(pg_tz_args())),
            shm_args=tuple(shlex.split(pg_shm_args())),
            archive_args=tuple(archive_pg_args(path=config.walg_config_file, home=home)),
        )
    )

    wait = PostmasterWait(pg_isready, pg_port, dial_host, host=host, timeout_s=tuning.pg_probe_s)
    try:
        owned_postgres.start(
            data,
            pg_port,
            argv,
            pg_start_env(environ),
            ready=wait,
            timeout=tuning.pg_start_s,
            expected=owner,
            retained_children=retained_children,
            timers=timers,
            system=system,
        )
    except owned_postgres.PostgresNotReadyError as exc:
        # meiva:note
        # A probe that answered leaves the custody boundary's literal alone; one that never answered
        # adds its own words, so a broken probe is never read as a slow postmaster.
        raise owned_postgres.PostgresNotReadyError(launch_failure(wait, exc)) from exc


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
    host: Host,
    retained_children: list[subprocess.Popen[bytes]],
    machine: MachineIdentityReader,
    tuning: StartTuning = PRODUCTION_TUNING,
    timers: Timers,
    system: str,
    environ: Mapping[str, str],
) -> int:
    data = pg_data_dir(home)
    owner = ownership.require_postgres(data, pg_port, required=False)
    identity = _identity()
    dial_host = url_host(config.db_url)
    off_box = off_box_host(machine)
    for step in pg_plan.plan_pg_start(owner_retained=owner is not None, off_box_host=off_box):
        match step:
            case pg_plan.EnsureData():
                ensure_pg_data(home, os_user=identity.os_user, host=host)
            case pg_plan.WriteAuthentication():
                (data / "pg_ident.conf").write_text(pg_plan.pg_ident_body(identity))
                hba = pg_plan.pg_hba_body(
                    identity, reachable_host=off_box, trusted_cidrs=config.trusted_cidrs
                )
                (data / "pg_hba.conf").write_text(hba)
            case pg_plan.AwaitReachableBind(host=reachable):
                if not await_bind("postgres", reachable, tuning, timers=timers):
                    return 1
            case pg_plan.LaunchPostmaster():
                _launch_postmaster(
                    config,
                    pg_port,
                    home=home,
                    host=host,
                    dial_host=dial_host,
                    machine=machine,
                    retained_children=retained_children,
                    owner=owner,
                    tuning=tuning,
                    timers=timers,
                    system=system,
                    environ=environ,
                )
            case pg_plan.ProveAuthentication():
                ownership.require_postgres(data, pg_port)
                require_authenticated_hba(
                    pg_port, dial_host, socket_dir=pg_socket_dir(home), timers=timers
                )
                print(
                    f"  ✓ postgres ready ({dial_host}:{pg_port}, password authentication enforced)"
                )
            case pg_plan.CheckArchive():
                warn_archive_inactive(config.walg_config_file, home=home)
            case _:
                assert_never(step)
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
    host: Host,
    pg_port: int,
    redis_port: int,
    redis_password: str,
    redis_user: str,
    retained_children: list[subprocess.Popen[bytes]],
    machine: MachineIdentityReader,
    system: str,
    timers: Timers,
    environ: Mapping[str, str],
) -> int:
    require_storage_credentials(config.redis_admin_password, redis_password)
    print(f"\n→ per-cluster data plane (pg :{pg_port}, redis :{redis_port})")
    rc = start_pg(
        config,
        pg_port,
        home=home,
        host=host,
        retained_children=retained_children,
        machine=machine,
        timers=timers,
        system=system,
        environ=environ,
    )
    if rc != 0:
        return rc
    return start_redis(
        config,
        redis_port,
        redis_password,
        redis_user,
        home=home,
        host=host,
        machine=machine,
        macos=system == "darwin",
        timers=timers,
        environ=environ,
    )
