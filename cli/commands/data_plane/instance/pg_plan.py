# meiva:note
# How this cluster's own Postgres is authenticated, launched and started.
#
# Everything a start decides is a value computed here: the always-authenticated pg_hba and
# the pg_ident map, the postmaster's command line, and the ordered steps a start performs
# given whether a retained postmaster already owns the data directory. The shell in
# `cluster_instance.py` reads the OS user, resolves the binaries and the addresses, executes
# each step and stops at the first one that fails.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AwaitReachableBind",
    "CheckArchive",
    "EnsureData",
    "LaunchPostmaster",
    "PgIdentity",
    "PgLaunch",
    "PgStep",
    "ProveAuthentication",
    "WriteAuthentication",
    "bind_refusal",
    "hba_dial_verdict",
    "pg_hba_body",
    "pg_ident_body",
    "pg_launch_argv",
    "plan_pg_start",
]


# meiva:note
# The names the authentication files grant access to.
#
# `os_user` is the initdb bootstrap superuser, who reaches Postgres only through `peer` on the
# owner-only socket. `monitor_role` is the password-less statistics login the same OS user may
# assume through the `pg_ident` map named `monitor_map`.
@dataclass(frozen=True)
class PgIdentity:
    os_user: str
    monitor_role: str
    monitor_map: str


# meiva:note
# Always-authenticated pg_hba.
#
# The OS user reaches Postgres only through `peer` on the owner-only socket, the
# administrator authority that provisioning, migrations and the authority invariant use. The
# same OS user reaches the monitoring login through `peer` with the `pg_ident` map. Every other
# role authenticates with SCRAM over the socket and loopback TCP; NOLOGIN roles never log in
# under any method. The declared reach adds SCRAM host lines: the reachable address when the
# machine declares one (`reachable_host`, `None` on a single box) and each trusted CIDR.
def pg_hba_body(identity: PgIdentity, *, reachable_host: str | None, trusted_cidrs: str) -> str:
    lines = [
        f"local all {identity.os_user} peer",
        f"local all {identity.monitor_role} peer map={identity.monitor_map}",
        "local all all scram-sha-256",
        "host all all 127.0.0.1/32 scram-sha-256",
        "host all all ::1/128 scram-sha-256",
    ]
    if reachable_host is not None:
        lines.append(f"host all all {reachable_host}/32 scram-sha-256")
    for cidr in (c.strip() for c in trusted_cidrs.split(",") if c.strip()):
        lines.append(f"host all all {cidr} scram-sha-256")
    return "\n".join(lines) + "\n"


# meiva:note
# The only ident mapping: this OS user may log in as the monitoring role.
def pg_ident_body(identity: PgIdentity) -> str:
    return f"{identity.monitor_map} {identity.os_user} {identity.monitor_role}\n"


# meiva:note
# Everything the postmaster's command line is made of.
#
# The settings fragments (`tz_args`, `shm_args`, `archive_args`) arrive already split into
# arguments, in the order they were produced, and are appended after the fixed settings.
@dataclass(frozen=True)
class PgLaunch:
    postgres_bin: str
    data: Path
    port: int
    listen_addrs: tuple[str, ...]
    socket_dir: Path
    max_connections: int
    tz_args: tuple[str, ...]
    shm_args: tuple[str, ...]
    archive_args: tuple[str, ...]


# meiva:note
# The postmaster command line: direct `postgres`, listening on the declared addresses with an
# owner-only unix socket.
def pg_launch_argv(launch: PgLaunch) -> list[str]:
    return [
        launch.postgres_bin,
        "-D",
        str(launch.data),
        "-p",
        str(launch.port),
        "-c",
        f"listen_addresses={','.join(launch.listen_addrs)}",
        "-c",
        f"unix_socket_directories={launch.socket_dir}",
        "-c",
        "unix_socket_permissions=0700",
        "-c",
        f"max_connections={launch.max_connections}",
        *launch.tz_args,
        *launch.shm_args,
        *launch.archive_args,
    ]


# meiva:note
# Initialize the data directory from the cached template when it has no PG_VERSION.
@dataclass(frozen=True)
class EnsureData: ...


# meiva:note
# Write the pg_ident and pg_hba files before anything serves.
@dataclass(frozen=True)
class WriteAuthentication: ...


# meiva:note
# Wait for the machine's declared reachable address to be assigned to a local interface.
@dataclass(frozen=True)
class AwaitReachableBind:
    host: str


# meiva:note
# Launch the postmaster, or complete readiness of the retained one, under native custody.
@dataclass(frozen=True)
class LaunchPostmaster: ...


# meiva:note
# Prove the running postmaster enforces password authentication.
@dataclass(frozen=True)
class ProveAuthentication: ...


# meiva:note
# Warn when WAL archiving is configured but the retained postmaster is not carrying it.
@dataclass(frozen=True)
class CheckArchive: ...


PgStep = (
    EnsureData
    | WriteAuthentication
    | AwaitReachableBind
    | LaunchPostmaster
    | ProveAuthentication
    | CheckArchive
)


# meiva:note
# The ordered steps of one Postgres start.
#
# Initialization and the authentication files come first, so a loaded hba is never the
# initdb default. The bind wait applies only to a fresh launch on a machine that declared a
# reachable address (`off_box_host`); a retained postmaster already holds its listeners.
def plan_pg_start(*, owner_retained: bool, off_box_host: str | None) -> tuple[PgStep, ...]:
    wait: tuple[PgStep, ...] = (
        (AwaitReachableBind(off_box_host),)
        if off_box_host is not None and not owner_retained
        else ()
    )
    return (
        EnsureData(),
        WriteAuthentication(),
        *wait,
        LaunchPostmaster(),
        ProveAuthentication(),
        CheckArchive(),
    )


# meiva:note
# Why a start refuses when the reachable bind address never appeared.
def bind_refusal(service: str, host: str, waited_s: int) -> str:
    return (
        f"  ✗ reachable bind address {host!r} is not assigned to any local interface after "
        f"{waited_s}s — {service} cannot bind it. On reboot this means the private network has "
        "not come up yet; retry `meiva start --all-services` (or this unit's own service "
        "selection) once it is."
    )


# meiva:note
# The server asks a password-less client for one; libpq reports either wording.
_PASSWORD_DEMANDED = ("no password supplied", "password authentication failed")


# meiva:note
# What one password-less dial proves about the running postmaster's pg_hba.
#
# `pg_hba_file_rules` shows the file, not what the postmaster loaded, and a retained postmaster
# reloads asynchronously after SIGHUP, so the proof is behavioral: a password-less dial as a role
# name that cannot exist must be asked for a password. A trust line would instead answer that the
# role does not exist. `error` is the dial's failure text, `None` when it connected. The result
# is `None` when `host` demanded a password, else why it is unproven.
def hba_dial_verdict(host: str, error: str | None) -> str | None:
    if error is None:
        return f"{host}: admitted without a password"
    if any(marker in error for marker in _PASSWORD_DEMANDED):
        return None
    lines = error.strip().splitlines()
    return f"{host}: {lines[-1] if lines else 'connection failed'}"
