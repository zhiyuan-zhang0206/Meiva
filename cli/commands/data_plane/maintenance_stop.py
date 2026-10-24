# meiva:note
# Verified native data-plane stop for a unit whose services have already stopped.
#
# No force escalation of the pooler or Redis, no snapshots, no remote management. A Postgres
# fast shutdown that outlives its share of the budget is ended by an immediate shutdown
# (docs/decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md). The stop semantics
# match what the preceding services stop has already proven: the pooler gets SIGINT (safe
# shutdown: it disconnects clients and waits only for in-flight server transactions) and
# PostgreSQL native fast shutdown (disconnect idle sessions, roll back stragglers, checkpoint),
# so neither waits on idle client connections a paused runner may still hold (issue #2307).
# Redis saves its current in-memory data before shutdown. PID disappearance is checked in
# addition to command completion; an uncertain result always fails the stop.
#
# The shell identifies every captured service before the first stop, asks `maintenance/plan.py`
# in which order to stop them and `maintenance/process_scan.py` whether anything of this home is
# unrecorded, and executes the answers.

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never, cast

import psutil
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from base.cluster import ownership
from base.cluster import postgres as owned_postgres
from base.cluster.dataplane import pooler as pooler_files
from base.host.db_authority.delivery import read_pooler_admin
from base.log import logger
from base.native_process.ownership import OwnedProcess, capture_tree
from base.telemetry import EventPipeline
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.maintenance.plan import (
    ShutdownRedis,
    StopPooler,
    StopPostgres,
    StopStep,
    fast_shutdown_reserve_s,
    plan_stop,
)
from cli.commands.data_plane.maintenance.process_scan import (
    SCANNED_NAMES,
    SeenProcess,
    judge,
    refusal,
    service_of,
)
from cli.commands.data_plane.maintenance.report_lines import (
    pooler_clients_line,
    pooler_console_unreadable_line,
)
from cli.commands.data_plane.stop_budget import postgres_fast_budget_s
from cli.commands.data_plane.stop_waits import (
    DEFAULT_STOP_LEGS,
    StopLegs,
    deadline_after,
    remaining,
    report_postgres_stop_escalation,
    wait_for_exit,
)

# meiva:note
# How many clients the pooler line names before it counts the rest.
_CLIENTS_LISTED = 10

# meiva:note
# The pause before a candidate whose attributing path could not be read is read once more.
_REREAD_PAUSE_S = 0.05


def _working_directory(process: psutil.Process) -> str | None:
    try:
        return str(Path(process.cwd()).resolve())
    except (psutil.AccessDenied, OSError):
        return None


# meiva:note
# The path that attributes `process` to a home, or None when it cannot be read. A read that fails
# is made once more after a short pause, because a process that is exiting (or exec-ing) refuses
# its working directory for an instant and then is gone: the second read raises NoSuchProcess and
# the candidate drops out, where a process that stays unreadable is reported as that.
def _locator(process: psutil.Process, name: str) -> str | None:
    read = pooler.config_of if service_of(name) == "pgbouncer" else _working_directory
    found = read(process)
    if found is None:
        time.sleep(_REREAD_PAUSE_S)
        found = read(process)
    return found


# meiva:note
# One candidate process as the process table shows it now. A process that exits while it is
# read is not a candidate; one whose uid or attributing path cannot be read is reported as that.
def look_at(process: psutil.Process, name: str) -> SeenProcess:
    try:
        if process.uids().real != os.getuid():
            return SeenProcess(process.pid, name, other_user=True, exiting=False, locator=None)
        exiting = process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.AccessDenied:
        return SeenProcess(process.pid, name, other_user=False, exiting=False, locator=None)
    if exiting:
        return SeenProcess(process.pid, name, other_user=False, exiting=True, locator=None)
    return SeenProcess(
        process.pid, name, other_user=False, exiting=False, locator=_locator(process, name)
    )


def _seen_processes() -> tuple[SeenProcess, ...]:
    seen: list[SeenProcess] = []
    for process in psutil.process_iter(["pid", "name"]):
        name = process.info["name"]
        if name not in SCANNED_NAMES:
            continue
        try:
            seen.append(look_at(process, name))
        except psutil.NoSuchProcess:
            continue
    return tuple(seen)


# meiva:note
# Refuse while a data-plane process of this home is running that the stop has not recorded, or
# one that cannot be attributed to any home.
#
# A missing pidfile or refused port is not evidence of no local process, so the process table
# is scanned too. `recorded` maps each service to the pids of its captured process and
# descendants (PostgreSQL worker processes share the postmaster's data directory).
def require_no_unrecorded(recorded: Mapping[str, frozenset[int]], *, home: Path) -> None:
    homes = {
        "postgres": str(instance.pg_data_dir(home).resolve()),
        "redis": str(instance.redis_data_dir(home).resolve()),
        "pgbouncer": str(pooler_files.ini_path().resolve()),
    }
    problem = refusal(judge(_seen_processes(), homes=homes, recorded=recorded))
    if problem is not None:
        raise RuntimeError(problem)


# meiva:note
# Say who is still connected to the pooler when it is about to stop (report only); the line is
# returned for the stop journal, None when nobody is connected.
#
# The pooler stops by SIGINT, which disconnects its clients, so nothing here gates or changes
# the stop: the line (stderr, log, and the returned value) names the remote clients a
# coordinated stop should already have stopped first. A console that cannot be read is reported
# as that, never as an empty list, whatever made it unreadable.
def report_pooler_clients(identity: OwnedProcess, *, home: Path) -> str | None:
    try:
        port = OwnedPooler.from_config(identity, pooler_files.ini_path()).port
        found = pooler_files.clients(port, read_pooler_admin(home).password)
    except Exception as exc:
        line = pooler_console_unreadable_line(repr(exc))
    else:
        described = pooler_clients_line(
            [client.describe() for client in found], shown=_CLIENTS_LISTED
        )
        if described is None:
            logger.info("pooler has no clients at its stop")
            return None
        line = described
    print(f"  ! {line}", file=sys.stderr, flush=True)
    logger.warning(line)
    return line


# meiva:note
# One Redis command bound by the stop's deadline. redis-py's command stubs are partially unknown;
# the pyright directive says only that.
async def _redis_command(client: Redis, deadline: float, *args: str) -> object:
    return cast(
        "object",
        await asyncio.wait_for(client.execute_command(*args), remaining(deadline)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    )


# meiva:note
# What one maintenance stop needs besides the services it ends: the home whose data directory
# it acts on, the bounds of the legs after Postgres' graceful stop, and the telemetry producer.
@dataclass(frozen=True)
class StopContext:
    home: Path
    legs: StopLegs
    producer: Callable[[], EventPipeline]


# meiva:note
# What one maintenance stop did: the services it ended in order, the note of each shutdown it had
# to escalate, and the line naming the clients the pooler still held when it stopped.
@dataclass(frozen=True)
class MaintenanceStop:
    stopped: tuple[str, ...]
    notes: tuple[str, ...]
    clients: tuple[str, ...]


# meiva:note
# How long Postgres' fast shutdown may take: what is left of the stop's deadline minus the legs
# that follow it, so a stuck fast shutdown cannot spend the whole shared budget.
#
# The reserve is the immediate shutdown's wait, the SIGKILL wait, and the same cleanup wait for
# Redis' save. A stop whose remaining time is not larger than the reserve gives the fast
# shutdown all of it; the escalation legs then overrun the deadline by their own bounded waits,
# like the shell-session closure's SIGKILL leg.
def postgres_fast_budget(deadline: float, legs: StopLegs) -> float:
    reserve = fast_shutdown_reserve_s(legs.cleanup_wait_s, legs.kill_wait_s)
    return postgres_fast_budget_s(remaining(deadline), reserve)


# meiva:note
# End one service. The Postgres leg returns the note of a shutdown it had to escalate, for the
# stop journal; every other leg returns None.
async def request_stop(
    step: StopStep,
    identity: OwnedProcess,
    client: Redis,
    deadline: float,
    context: StopContext,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> str | None:
    match step:
        case StopPostgres():
            # meiva:note
            # SIGINT requests PostgreSQL's fast, checkpointed shutdown; a fast shutdown stuck
            # past its budget (an archive command that never returns) is ended by SIGQUIT.
            escalation = owned_postgres.stop(
                instance.pg_data_dir(context.home),
                expected=identity,
                timeout=postgres_fast_budget(deadline, context.legs),
                immediate_wait=context.legs.cleanup_wait_s,
                kill_wait=context.legs.kill_wait_s,
                retained_children=retained_children,
            )
            if escalation is not None:
                return report_postgres_stop_escalation(escalation, producer=context.producer)
        case ShutdownRedis(mode):
            # meiva:note
            # Do not use redis-py's shutdown helper: it accepts any connection error as
            # success. The expected EOF is accepted only because the exact PID is then waited
            # for; a timeout or any other command failure is loud.
            with contextlib.suppress(RedisConnectionError):
                await _redis_command(client, deadline, "SHUTDOWN", mode.value)
        case StopPooler():
            custodian = OwnedPooler.from_config(identity, pooler_files.ini_path())
            if not custodian.stop(deadline=deadline):
                raise TimeoutError("PgBouncer stop incomplete; custody retained")
        case _:
            assert_never(step)
    return None


# meiva:note
# The dedicated admin connection of the stop. The default user is the native instance's admin
# identity; the runtime URL can carry a restricted ACL user's different password, which is not
# admin auth. Every wait of the client is bound by what is left of the stop's deadline.
def _admin_client(config: DataPlaneConfig, port: int, deadline: float) -> Redis:
    return Redis(
        host="127.0.0.1",
        port=port,
        password=config.redis_admin_password or None,
        decode_responses=True,
        single_connection_client=True,
        socket_connect_timeout=remaining(deadline),
        socket_timeout=remaining(deadline),
        retry=Retry(NoBackoff(), 0),
    )


# meiva:note
# Cleanup must not wait another full socket timeout after the shared deadline. Close this
# private transport without waiting for peer EOF.
async def _release(client: Redis, deadline: float) -> None:
    if client.connection is not None:
        await client.connection.disconnect(nowait=True)
    await asyncio.wait_for(client.aclose(), max(0.001, deadline - time.monotonic()))


def _require_nothing_left(port: int, deadline: float, home: Path) -> None:
    remaining(deadline)
    if (
        ownership.postgres(instance.pg_data_dir(home)) is not None
        or ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path()) is not None
    ):
        raise RuntimeError("data-plane process appeared during held stop")
    require_no_unrecorded({}, home=home)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=remaining(deadline)):
            raise RuntimeError("Redis endpoint still accepts connections after held stop")
    except ConnectionRefusedError:
        return


async def _end_services(
    steps: tuple[StopStep, ...],
    captured: Mapping[str, OwnedProcess],
    trees: Mapping[str, set[OwnedProcess]],
    client: Redis,
    deadline: float,
    context: StopContext,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> MaintenanceStop:
    stopped: list[str] = []
    notes: list[str] = []
    clients: list[str] = []
    for step in steps:
        name, identity = step.service, captured[step.service]
        remaining(deadline)
        if not identity.live():
            raise RuntimeError(f"{name} identity changed before stop")
        if (
            isinstance(step, StopPooler)
            and (line := report_pooler_clients(identity, home=context.home)) is not None
        ):
            clients.append(line)
        note = await request_stop(
            step, identity, client, deadline, context, retained_children=retained_children
        )
        if note is not None:
            notes.append(note)
        wait_for_exit(trees[name], deadline)
        stopped.append(name)
    return MaintenanceStop(tuple(stopped), tuple(notes), tuple(clients))


async def _stop(
    config: DataPlaneConfig,
    deadline: float,
    context: StopContext,
    *,
    save: bool,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> MaintenanceStop:
    pg = ownership.postgres(instance.pg_data_dir(context.home))
    pgb = ownership.pooler(pooler_files.ini_path(), pooler_files.pidfile_path())
    port = ownership.redis_port_of(config.redis_url)
    if port is None:
        raise RuntimeError("maintenance requires this home's explicit Redis endpoint")
    client = _admin_client(config, port, deadline)
    custody = ownership.RedisConnectionCustody()
    try:
        redis_process = await custody.capture(
            client, port=port, data_dir=instance.redis_data_dir(context.home), deadline=deadline
        )
        captured = {
            name: process
            for name, process in (("pgbouncer", pgb), ("postgres", pg), ("redis", redis_process))
            if process is not None
        }
        trees = {name: capture_tree(process) for name, process in captured.items()}
        require_no_unrecorded(
            {name: frozenset(member.pid for member in tree) for name, tree in trees.items()},
            home=context.home,
        )
        # meiva:note
        # Every service is identified before the first stop; no foreign Redis endpoint can be
        # discovered only after the local database was stopped.
        stopped = await _end_services(
            plan_stop(frozenset(captured), save=save),
            captured,
            trees,
            client,
            deadline,
            context,
            retained_children=retained_children,
        )
        _require_nothing_left(port, deadline, context.home)
        return stopped
    except RedisError as exc:
        raise RuntimeError(f"Redis maintenance stop failed ({type(exc).__name__})") from None
    finally:
        await _release(client, deadline)


def stop(
    config: DataPlaneConfig,
    timeout: float,
    *,
    home: Path,
    save: bool = True,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
    legs: StopLegs = DEFAULT_STOP_LEGS,
) -> MaintenanceStop:
    deadline = deadline_after(timeout)
    if config.is_remote:
        raise RuntimeError("maintenance cannot verify a remote-managed data-plane stop")
    return asyncio.run(
        _stop(
            config,
            deadline,
            StopContext(home, legs, producer),
            save=save,
            retained_children=retained_children,
        )
    )
