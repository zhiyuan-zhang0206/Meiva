# meiva:note
# How this cluster's own Redis is configured, launched and started.
#
# Redis always authenticates, whatever the bearer: `requirepass` is the gateway-only admin
# password, rendered into a 0600 `redis.conf` so it never reaches argv, and the cluster's ACL
# user has its own runtime password. This module renders the conf and the command line, checks
# the credentials, and orders the steps of a start given whether Redis already answers. The
# shell in `cluster_instance.py` resolves the binaries and addresses and executes the steps.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AffirmAcl",
    "AwaitReachableBind",
    "AwaitServerReady",
    "LaunchServer",
    "PrepareDataDir",
    "RedisLaunch",
    "RedisStep",
    "ReportReady",
    "RequireFreePort",
    "WriteConf",
    "plan_redis_start",
    "redis_auth_env",
    "redis_bind_addrs",
    "redis_conf_text",
    "redis_server_argv",
    "require_redis_credentials",
]

# meiva:note
# Redis default persistence schedule (900s/1 change, 300s/10, 60s/10000).
_SAVE_SCHEDULE = ("900 1", "300 10", "60 10000")


# meiva:note
# The `redis.conf` body: the RDB save schedule and `requirepass`.
#
# A restart without persistence wipes the frozen caches and re-causes the all-at-once rebuild
# stampede (2026-08-29 #2004 amplifier, task #2027), so the schedule lives in the conf and
# survives every render rather than riding a runtime CONFIG SET. Everything else stays a
# flag: redis applies flags after the config file, so the per-cluster port, bind and dir
# still win and a stale conf from an older start cannot pin them.
#
# Raises ValueError when the admin password is empty: Redis always authenticates.
def redis_conf_text(admin_password: str) -> str:
    if not admin_password:
        raise ValueError("redis.conf requires the Redis admin password (requirepass)")
    saves = "".join(f"save {spec}\n" for spec in _SAVE_SCHEDULE)
    escaped = admin_password.replace("\\", "\\\\").replace('"', '\\"')
    return saves + f'requirepass "{escaped}"\n'


# meiva:note
# The child-only environment that authenticates `redis-cli` without a secret on its command
# line: `-a <secret>` is argv, which `ps` shows to any local user (issue #974), while
# `$REDISCLI_AUTH` is redis-cli's own answer to exactly this.
#
# Raises ValueError when the admin password is empty.
def redis_auth_env(admin_password: str) -> dict[str, str]:
    if not admin_password:
        raise ValueError("the owned Redis always requires its admin password")
    return {"REDISCLI_AUTH": admin_password}


# meiva:note
# Redis always authenticates, so a start without both generated passwords is refused before
# any effect, with the bring-up's own wording.
def require_redis_credentials(admin_password: str, runtime_password: str) -> None:
    if not (admin_password and runtime_password):
        raise ValueError("Redis always authenticates: its admin and runtime passwords are required")


# meiva:note
# The addresses Redis binds: loopback alone on macOS (its workaround, with the root-supervised
# relay serving the private network), else the machine's declared bind addresses.
def redis_bind_addrs(*, macos: bool, declared: tuple[str, ...]) -> tuple[str, ...]:
    return ("127.0.0.1",) if macos else declared


# meiva:note
# What the `redis-server` command line is made of; the password is in the conf, never here.
@dataclass(frozen=True)
class RedisLaunch:
    server_bin: str
    conf: Path
    port: int
    bind_addrs: tuple[str, ...]
    data: Path


# meiva:note
# The daemonized `redis-server` command line.
def redis_server_argv(launch: RedisLaunch) -> list[str]:
    return [
        launch.server_bin,
        str(launch.conf),
        "--daemonize",
        "yes",
        "--port",
        str(launch.port),
        "--bind",
        *launch.bind_addrs,
        "--protected-mode",
        "no",
        "--dir",
        str(launch.data),
        "--logfile",
        str(launch.data / "redis.log"),
    ]


# meiva:note
# Re-affirm the cluster's ACL user, which a restart that drops the in-memory ACL loses.
@dataclass(frozen=True)
class AffirmAcl: ...


# meiva:note
# Render the conf into the data directory (0600).
@dataclass(frozen=True)
class WriteConf: ...


# meiva:note
# Refuse when a foreign process listens on the port.
@dataclass(frozen=True)
class RequireFreePort: ...


# meiva:note
# Wait for the machine's declared reachable address to be assigned to a local interface.
@dataclass(frozen=True)
class AwaitReachableBind:
    host: str


# meiva:note
# Create the data directory.
@dataclass(frozen=True)
class PrepareDataDir: ...


# meiva:note
# Run `redis-server --daemonize yes`.
@dataclass(frozen=True)
class LaunchServer: ...


# meiva:note
# `redis-server --daemonize` returns at once; wait for the port to answer.
@dataclass(frozen=True)
class AwaitServerReady: ...


# meiva:note
# Say Redis is up: `already_running` distinguishes a re-affirmed instance from a fresh start.
@dataclass(frozen=True)
class ReportReady:
    already_running: bool


RedisStep = (
    AffirmAcl
    | WriteConf
    | RequireFreePort
    | AwaitReachableBind
    | PrepareDataDir
    | LaunchServer
    | AwaitServerReady
    | ReportReady
)


# meiva:note
# The ordered steps of one Redis start.
#
# A running Redis only has its ACL user and conf re-affirmed. A stopped one is launched: a
# foreign listener refuses first, then (off macOS, where Redis binds loopback alone) the
# declared reachable address is awaited, then the conf and the daemon, and the ACL user is
# provisioned last because it needs the running server.
def plan_redis_start(
    *, running: bool, macos: bool, off_box_host: str | None
) -> tuple[RedisStep, ...]:
    if running:
        return (AffirmAcl(), WriteConf(), ReportReady(already_running=True))
    wait: tuple[RedisStep, ...] = (
        (AwaitReachableBind(off_box_host),) if off_box_host is not None and not macos else ()
    )
    return (
        RequireFreePort(),
        *wait,
        PrepareDataDir(),
        WriteConf(),
        LaunchServer(),
        AwaitServerReady(),
        AffirmAcl(),
        ReportReady(already_running=False),
    )
