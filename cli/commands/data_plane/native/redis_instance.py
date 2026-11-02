# meiva:note
# Start, probe and stop this home's native Redis — the shell of `instance/redis_plan.py`.
#
# That module decides the configuration, the command line and the ordered steps; this one reads
# the binaries, executes each step and stops at the first one that fails. Every path is derived
# from the `home` the caller hands in.
#
# Redis ALWAYS authenticates, whatever the bearer: `requirepass` = the gateway-only Redis admin
# password, and the cluster's ACL user has its own runtime password. Both are minted at first
# start; a home without them is refused, never converted. The admin password reaches redis through
# a 0600 `redis.conf` and reaches `redis-cli` through `$REDISCLI_AUTH` — never argv, which `ps`
# shows to any local user (issue #974).


from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.clock import Timers
from base.cluster import ensure_cluster_redis_acl, port_preflight
from base.cluster.machine import MachineIdentityReader
from base.host.brew_pin import brew_prefix
from base.host.net.url_secret import url_host
from base.host.private_storage import write_private_bytes
from base.host.system.hostport import Host
from base.native_process.child_env import filter_daemon_env, inherit_env
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance import redis_plan
from cli.commands.data_plane.instance.bounded_wait import Wait, wait_verdict
from cli.commands.data_plane.instance.redis_binaries import (
    BrewFormula,
    OnPath,
    OverrideDir,
    redis_binary_source,
)
from cli.commands.data_plane.native.start_bounds import (
    PRODUCTION_TUNING,
    StartTuning,
    await_bind,
    off_box_host,
)

_REDIS_PROBE_TIMEOUT_S = 5.0
_REDIS_LAUNCH_TIMEOUT_S = 30.0
_REDIS_READY_TIMEOUT_S = 6.0
_REDIS_READY_INTERVAL_S = 0.1


# meiva:note
# This home's native Redis data directory.
def redis_data_dir(home: Path) -> Path:
    return home / "redis"


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
# The child env that authenticates `redis-cli` without a secret on its command line.
#
# Raises ValueError when the admin password is empty: the owned Redis always requires it.
def redis_cli_env(redis_admin_password: str, *, environ: Mapping[str, str]) -> dict[str, str]:
    return inherit_env(environ, redis_plan.redis_auth_env(redis_admin_password))


def redis_running(
    redis_cli: str,
    redis_admin_password: str,
    redis_port: int,
    redis_host: str,
    *,
    host: Host,
    environ: Mapping[str, str],
) -> bool:
    try:
        out = host.run(
            [redis_cli, "-h", redis_host, "-p", str(redis_port), "ping"],
            env=redis_cli_env(redis_admin_password, environ=environ),
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
    machine: MachineIdentityReader
    tuning: StartTuning
    macos: bool
    redis_cli: str
    dial_host: str
    host: Host
    timers: Timers
    environ: Mapping[str, str]

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
                return 0 if await_bind("redis", host, self.tuning, timers=self.timers) else 1
            case redis_plan.PrepareDataDir():
                self.data.mkdir(parents=True, exist_ok=True)
            case redis_plan.LaunchServer():
                launched = _launch_redis(
                    self.config,
                    self.redis_port,
                    self.data,
                    self.machine,
                    host=self.host,
                    macos=self.macos,
                    environ=self.environ,
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
        if _await_redis(
            self.redis_cli,
            admin,
            self.redis_port,
            self.dial_host,
            host=self.host,
            timers=self.timers,
            environ=self.environ,
        ):
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
    host: Host,
    machine: MachineIdentityReader,
    macos: bool,
    tuning: StartTuning = PRODUCTION_TUNING,
    timers: Timers,
    environ: Mapping[str, str],
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
        host=host,
        timers=timers,
        environ=environ,
    )
    running = redis_running(redis_cli, admin, redis_port, dial_host, host=host, environ=environ)
    steps = redis_plan.plan_redis_start(
        running=running, macos=macos, off_box_host=off_box_host(machine)
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
    config: DataPlaneConfig,
    redis_port: int,
    data: Path,
    machine: MachineIdentityReader,
    *,
    macos: bool,
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
    config: DataPlaneConfig,
    redis_port: int,
    data: Path,
    machine: MachineIdentityReader,
    *,
    host: Host,
    macos: bool,
    environ: Mapping[str, str],
) -> bool:
    result = host.run(
        redis_launch_argv(config, redis_port, data, machine, macos=macos),
        capture_output=True,
        text=True,
        env=filter_daemon_env(environ),
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
def _await_redis(
    redis_cli: str,
    admin: str,
    redis_port: int,
    dial_host: str,
    *,
    host: Host,
    timers: Timers,
    environ: Mapping[str, str],
) -> bool:
    started = timers.monotonic()
    while True:
        verdict = wait_verdict(
            satisfied=redis_running(
                redis_cli, admin, redis_port, dial_host, host=host, environ=environ
            ),
            elapsed_s=timers.monotonic() - started,
            timeout_s=_REDIS_READY_TIMEOUT_S,
        )
        if verdict is Wait.DONE:
            return True
        if verdict is Wait.EXPIRED:
            return False
        timers.sleep(_REDIS_READY_INTERVAL_S)


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
