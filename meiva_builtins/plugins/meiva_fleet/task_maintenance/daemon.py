import asyncio
import logging
import os
import random
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.inbox.wake import publish_inbound_wake
from base.api_contracts.agent_contract import AgentStatus
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    Liveness,
    start_health_server,
    stop_health_server,
)
from base.daemon.shutdown import install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.db.rows import query_all
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugins.config_registration import (
    disk_image_path,
    read_config_image,
    read_service_config,
)
from base.paths import meiva_home, run_dir
from base.telemetry.process_logging import init_gateway_process
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from meiva_builtins.plugins.meiva_fleet.task_maintenance import routing, undelivered
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

# meiva:note
# Task-maintenance daemon — gateway-owned task reminders + escalation, one loop
# cluster-wide, registered into the ops service roster by the `meiva_fleet` plugin
# rather than hardcoded in `services/supervision/roster/spec.py`. Reminders and
# escalations insert into `inbound_messages` directly and then best-effort wake via
# Redis, modeled on heartbeat; counter updates stay direct DB writes. They never
# resurrect a terminated agent and never address one either: a dead owner's due
# reminder is rerouted to its manager, and when no live agent owns an ancestor task
# nothing is sent to anyone: the cadence advances and the sweep logs it (user ruling
# 2026-10-25 — a dead owner's queue had held the reminders nobody read — and the
# later removal of the human notice leg). The escalate pass fires once a task has gone
# `task_escalate_n` of its own cadence periods without a state change and at least
# one reminder reached its owner — counting cadence silence rather than delivered
# reminders, because the per-owner digest window throttles delivery and would
# otherwise stretch the wait (2026-11-07, task #6354). It reaches the same manager,
# and `escalated_at` keeps the
# delegator leg at most once per overdue window; a write this home's policy counts clears
# `escalated_at` with the reminder counters and re-arms it, and the default counts a note,
# so answering a reminder with one re-arms it (the criterion is
# `base/agents/tasks/reminder_window.py`). A schema-drift exit
# (`psycopg.ProgrammingError`) is revived by the root health monitor's `/healthz`
# probe on the next round instead of staying dead.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.md
_log = logging.getLogger("meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon")


_DAEMON_MODULE = "meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon"


# meiva:note
# The process's random generator, built once at its entry for the event bus's retry jitter. Jitter
# is not a secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


def task_maintenance_endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("task_maintenance")


_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0


# ── Reminder pass ──────────────────────────────────────────────────────────────

# meiva:note
# Tasks whose remind_interval_seconds has elapsed AND the owner hasn't been reminded
# within this overdue window (last_reminded_at gates it within
# max(backoff, remind_interval_seconds) — each task repeats at its own cadence).
# A task that still has an open direct child is a container, not a work item: its
# children carry the reminders, and repeating them on the parent adds nothing. A task
# whose owner parked it (`reminders_paused_until` in the future) is skipped here and in
# the escalation pass; when the deadline passes it is due at once, because the pause
# never moved `updated_at` and never cleared `reminder_count` — parking is not
# forgetting (task #5311).
# A terminated owner stays eligible here: its due reminder is rerouted rather
# than dropped, and the row lock is what stops a concurrent sweep from accepting
# the same cadence twice. `FOR NO KEY UPDATE` (not `FOR UPDATE`) keeps that lock
# from blocking a writer that references the task by FK — the same mode the escalate
# pass uses for the same reason. The join reads the owner's status without locking the
# identity row — messages take that lock themselves.
_REMINDER_SQL = """
    SELECT t.id, t.owner, t.title, t.remind_interval_seconds,
           EXTRACT(EPOCH FROM (now() - t.updated_at))::bigint AS elapsed,
           t.priority, m.status AS owner_status
    FROM agent_tasks t
    LEFT JOIN agents_meta m ON m.id = t.owner
    WHERE t.status = 'in_progress'
      AND t.owner IS NOT NULL
      AND t.remind_interval_seconds IS NOT NULL
      AND NOT t.is_root
      AND now() - t.updated_at > make_interval(secs => t.remind_interval_seconds)
      AND (t.reminders_paused_until IS NULL OR t.reminders_paused_until <= now())
      AND NOT EXISTS (
          SELECT 1 FROM agent_tasks child
          WHERE child.parent_id = t.id AND child.status = 'in_progress'
      )
      AND (
          t.last_reminded_at IS NULL
          OR now() - t.last_reminded_at
             > make_interval(secs => GREATEST(%s, t.remind_interval_seconds))
      )
      -- One digest per owner per window: the sweep runs every
      -- task_maintenance_interval_seconds, so an unbatched due task leaves as a message of
      -- its own (183/day measured 2026-11-06, 85 percent of them for four owners). The window is
      -- evaluated here, before the row is locked, so a held-back task is never selected and
      -- its reminder_count is never advanced: counting happens at delivery, not at holding.
      AND NOT EXISTS (
          SELECT 1 FROM inbound_messages m
          WHERE m.agent_id = t.owner
            AND m.source = 'system'
            AND m.payload->>'note_kind' = 'task_check_in'
            AND m.created_at > now() - make_interval(secs => CASE t.priority
                    WHEN 'P0' THEN %s::float8
                    WHEN 'P1' THEN %s::float8
                    ELSE %s::float8
                END)
      )
    ORDER BY t.priority, t.id
"""


# meiva:note
# A row of `_REMINDER_SQL`, named as its columns are. The statement filters on `owner` and
# `remind_interval_seconds` being non-null, and the `LEFT JOIN` leaves the owner's status null for
# an owner with no `agents_meta` row.
@dataclass(frozen=True)
class _DueRow:
    id: int
    owner: int
    title: str
    remind_interval_seconds: int
    elapsed: int
    priority: str
    owner_status: str | None


@dataclass(frozen=True)
class _DueTask:
    id: int
    owner: int
    title: str
    remind_interval_seconds: int
    elapsed_s: int
    priority: str


# meiva:note
# One manager's digest (grouped by the terminated owner that held the tasks), accepted in the
# sweep's transaction and announced after commit.
@dataclass(frozen=True)
class _Reroute:
    manager: int
    by_owner: Mapping[int, tuple[_DueTask, ...]]
    inbound_id: int
    content: str


# meiva:note
# A reminder is a task system notification (NoteTag 'task'), never peer chat (user
# ruling 2026-08-27), and its payload forbids a wake from resurrecting the owner.
# meiva:note
# The payload is the identity of a task notification, and `note_kind` is what the digest
# window reads. The rendered text is not an identity: rewording a message would silently
# stop the throttle and look exactly like "nothing is due today".
def _task_note_payload(note_kind: str) -> dict[str, object]:
    return {
        "note_tag": "task",
        "task_notification": True,
        "delivery_resurrect": False,
        "note_kind": note_kind,
    }


# meiva:note
# Cadence advances in the transaction that accepted the reminder. `delegated` also
# stamps escalated_at: the task's manager has just been told, and the escalation pass
# keys its delegator leg on that marker, so the same manager is not sent the same
# list again when reminder_count reaches the escalate threshold.
def advance_reminder_counters(
    cur: psycopg.Cursor, task_id: int, *, delegated: bool = False
) -> None:
    if delegated:
        cur.execute(
            "UPDATE agent_tasks SET last_reminded_at = now(), reminder_count = reminder_count + 1, "
            "escalated_at = now() WHERE id = %s",
            (task_id,),
        )
        return
    cur.execute(
        "UPDATE agent_tasks SET last_reminded_at = now(), reminder_count = reminder_count + 1 "
        "WHERE id = %s",
        (task_id,),
    )


def _task_line(task: _DueTask) -> str:
    return (
        f'- #{task.id} "{task.title}" — no update for {task.elapsed_s / 3600:.1f}h '
        f"(reminder interval: {task.remind_interval_seconds // 60}min)"
    )


# meiva:note
# The copy is a check-in, not an overdue verdict: each line orders the owner to report
# current status and push the next step, and to state why and raise the reminder
# interval when it truly cannot advance — an explicit wait instead of silent idling
# (user ruling 2026-08-29). The idle window and reminder interval stay on the line so
# the owner sees the silence it is being asked to break.
def _reminder_digest_message(tasks: list[_DueTask]) -> str:
    lines = [
        f"Task check-in — {len(tasks)} task(s) reached their reminder interval; "
        "check whether each needs a progress update or should be pushed forward:"
    ]
    for task in tasks:
        lines.append(
            f"{_task_line(task)}: if you cannot advance it, state why and adjust "
            "the reminder interval"
        )
    return "\n".join(lines)


# meiva:note
# Grouping by the terminated owner keeps a 60-task backlog to one message for the
# manager, and names who died so the manager can judge whether the work was already
# in hand elsewhere.
def _rerouted_digest_message(by_owner: dict[int, list[_DueTask]]) -> str:
    total = sum(len(tasks) for tasks in by_owner.values())
    lines = [
        f"Reminder rerouted — {total} task(s) due under {len(by_owner)} terminated owner(s); "
        "reassign each task to a live agent or cancel it:"
    ]
    for owner, tasks in sorted(by_owner.items()):
        lines.append(f"Agent #{owner} (terminated) — {len(tasks)} task(s):")
        lines.extend(_task_line(task) for task in tasks)
    return "\n".join(lines)


def _accept_owner_digests(
    cur: psycopg.Cursor, live: dict[int, list[_DueTask]]
) -> list[tuple[int, int, str, list[_DueTask]]]:
    accepted: list[tuple[int, int, str, list[_DueTask]]] = []
    for owner, tasks in sorted(live.items()):
        content = _reminder_digest_message(tasks)
        inbound_id, _ = insert_inbound_message_in_transaction(
            cur,
            owner,
            content,
            "system",
            kind="system_note",
            payload=_task_note_payload("task_check_in"),
        )
        for task in tasks:
            advance_reminder_counters(cur, task.id)
        accepted.append((owner, inbound_id, content, tasks))
    return accepted


def _accept_manager_digest(
    cur: psycopg.Cursor, manager: int, by_owner: dict[int, list[_DueTask]]
) -> _Reroute:
    content = _rerouted_digest_message(by_owner)
    inbound_id, _ = insert_inbound_message_in_transaction(
        cur,
        manager,
        content,
        "system",
        kind="system_note",
        payload=_task_note_payload("task_reroute"),
    )
    for tasks in by_owner.values():
        for task in tasks:
            advance_reminder_counters(cur, task.id, delegated=True)
    return _Reroute(
        manager, {owner: tuple(tasks) for owner, tasks in by_owner.items()}, inbound_id, content
    )


# meiva:note
# A terminated owner's due task with no live ancestor owner has no recipient: nothing is sent, the
# cadence still advances so the task waits its own window instead of repeating on every sweep, and
# the sweep logs the stranded task ids.
def _accept_reroutes(
    cur: psycopg.Cursor,
    orphaned: dict[int, list[_DueTask]],
) -> list[_Reroute]:
    managers = routing.nearest_manager(
        cur, [task.id for tasks in orphaned.values() for task in tasks]
    )
    managed: dict[int, dict[int, list[_DueTask]]] = defaultdict(lambda: defaultdict(list))
    stranded: dict[int, list[_DueTask]] = defaultdict(list)
    for owner, tasks in sorted(orphaned.items()):
        for task in tasks:
            manager = managers.get(task.id)
            if manager is None:
                stranded[owner].append(task)
            else:
                managed[manager][owner].append(task)
    reroutes = [
        _accept_manager_digest(cur, manager, by_owner)
        for manager, by_owner in sorted(managed.items())
    ]
    for owner, tasks in sorted(stranded.items()):
        for task in tasks:
            advance_reminder_counters(cur, task.id)
        _log.info(
            "[task-maintenance] no live manager for task(s) %s of terminated agent #%d; "
            "no reminder sent",
            [task.id for task in tasks],
            owner,
        )
    return reroutes


def _announce_owner_digests(
    digests: list[tuple[int, int, str, list[_DueTask]]], db: Database, bus: EventBus
) -> None:
    for owner, inbound_id, content, tasks in digests:
        announce_reminder(db, bus, owner, inbound_id, content)
        telemetry.emit(
            "telemetry",
            "task_reminder_digest",
            agent_id=owner,
            source="system",
            attributes={
                "owner_id": owner,
                "task_count": len(tasks),
                "task_ids": [task.id for task in tasks],
            },
        )


def _announce_reroutes(reroutes: list[_Reroute], db: Database, bus: EventBus) -> None:
    for reroute in reroutes:
        announce_reminder(db, bus, reroute.manager, reroute.inbound_id, reroute.content)
        for owner, tasks in sorted(reroute.by_owner.items()):
            telemetry.emit(
                "telemetry",
                "task_reminder_rerouted",
                agent_id=reroute.manager,
                source="system",
                attributes={
                    "owner_id": owner,
                    "leg": "manager",
                    "recipient": reroute.manager,
                    "task_count": len(tasks),
                    "task_ids": [task.id for task in tasks],
                },
            )


# meiva:note
# Delivery follows the owner's status. A live owner receives the check-in digest. A
# terminated owner cannot act, so each of its due tasks is rerouted to the manager
# `routing.nearest_manager` names — one digest per manager, grouped by the dead owner
# — or, when no ancestor has a live owner, nowhere. Both advance the cadence like any other
# reminder, so the next one waits its own window instead of repeating on every 5-minute sweep.
def run_reminders(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    backoff_seconds: float,
    *,
    digest_window_p0_seconds: float = 0.0,
    digest_window_p1_seconds: float = 0.0,
    digest_window_p2_seconds: float = 0.0,
    undelivered_grace_seconds: float = 0.0,
) -> int:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        live: dict[int, list[_DueTask]] = defaultdict(list)
        orphaned: dict[int, list[_DueTask]] = defaultdict(list)
        for due in query_all(
            conn,
            _DueRow,
            _REMINDER_SQL + " FOR NO KEY UPDATE OF t SKIP LOCKED",
            (
                backoff_seconds,
                digest_window_p0_seconds,
                digest_window_p1_seconds,
                digest_window_p2_seconds,
            ),
        ):
            task = _DueTask(
                due.id, due.owner, due.title, due.remind_interval_seconds, due.elapsed, due.priority
            )
            (orphaned if due.owner_status == AgentStatus.TERMINATED else live)[due.owner].append(
                task
            )
        digests = _accept_owner_digests(cur, live)
        reroutes = _accept_reroutes(cur, orphaned)
    undelivered.report_undelivered(pool, undelivered_grace_seconds)
    _announce_owner_digests(digests, db, bus)
    _announce_reroutes(reroutes, db, bus)
    if reroutes:
        _log.info("[task-maintenance] rerouted %d manager digest(s)", len(reroutes))
    return len(digests) + len(reroutes)


def announce_reminder(
    db: Database, bus: EventBus, owner: int, inbound_id: int, content: str
) -> None:
    """Best-effort hints after durable acceptance; watchdog heals a lost wake."""
    del content
    try:
        publish_agent_updated_sync(bus, owner)
        publish_inbound_wake(db, bus, owner, str(inbound_id))
    except Exception:
        _log.exception("[task-maintenance] committed reminder %s lost its live hint", inbound_id)


# ── Escalate pass ────────────────────────────────────────────────────────────


def run_escalate(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    escalate_n: int,
    backoff_seconds: float,
) -> int:
    # meiva:note
    # Accept current escalation state atomically, then announce committed work.
    from meiva_builtins.plugins.meiva_fleet.task_maintenance.escalation import accept_escalations

    receipts = accept_escalations(pool, escalate_n, backoff_seconds)
    for receipt in receipts:
        announce_reminder(db, bus, receipt.recipient, receipt.inbound_id, receipt.content)
        telemetry.emit(
            "telemetry",
            "task_escalation",
            agent_id=receipt.recipient,
            source="system",
            attributes={
                "owner_id": receipt.recipient,
                "task_count": len(receipt.task_ids),
                "task_ids": receipt.task_ids,
                "leg": receipt.leg,
            },
        )
    if receipts:
        _log.info("[task-maintenance] accepted %d escalation digests", len(receipts))
    return len(receipts)


# ── Daemon lifecycle ─────────────────────────────────────────────────────────


# meiva:note
# Whether a daemon is already running via its pidfile; pid-reuse-safe by verifying the live pid's
# argv names this daemon's module.
def is_running(pidfile: Path) -> bool:
    return pidfile_holds_daemon(pidfile, _DAEMON_MODULE)


async def _sleep_with_liveness(liveness: Liveness, total_s: float) -> None:
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def dispatch_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    liveness: Liveness,
    *,
    config: FleetConfig,
) -> None:
    interval = config.task_maintenance_interval_seconds
    backoff_seconds = config.task_reminder_backoff_seconds
    escalate_n = config.task_escalate_n
    _log.info(
        "[task-maintenance] daemon started, pid=%s, interval=%.0fs, backoff=%.0fs, escalate_n=%d",
        os.getpid(),
        interval,
        backoff_seconds,
        escalate_n,
    )
    while True:
        try:
            # meiva:note
            # The undelivered grace is one sweep to select a task plus the longest hold the digest
            # window can impose on its owner: past that a working delivery path has already counted
            # the task, so one still uncounted there is the anomaly the same sweep reports.
            run_reminders(
                pool,
                db,
                bus,
                backoff_seconds,
                digest_window_p0_seconds=config.task_reminder_digest_window_p0_seconds,
                digest_window_p1_seconds=config.task_reminder_digest_window_p1_seconds,
                digest_window_p2_seconds=config.task_reminder_digest_window_p2_seconds,
                undelivered_grace_seconds=interval
                + max(
                    config.task_reminder_digest_window_p0_seconds,
                    config.task_reminder_digest_window_p1_seconds,
                    config.task_reminder_digest_window_p2_seconds,
                ),
            )
            run_escalate(pool, db, bus, escalate_n, backoff_seconds)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[task-maintenance] schema / syntax error — code<->DB drift; "
                "retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[task-maintenance] poll iteration failed")
        # Sleep OUTSIDE the try: a transient failure waits a full interval
        # before retrying instead of hot-looping against Postgres (same
        # discipline as services/upkeep/events_maintenance/daemon.py — audit
        # round 2, P1: the sleep used to sit inside the try, so a
        # non-ProgrammingError exception skipped it and the loop spun).
        await _sleep_with_liveness(liveness, interval)


async def run(
    config: FleetConfig,
    *,
    database: Callable[[], Database],
    bus: EventBus,
    image: LoadedCommit,
    endpoint: ServiceEndpoint,
) -> None:
    if is_running(endpoint.pidfile):
        _log.info(
            "[task-maintenance] daemon already running (pidfile=%s), exiting", endpoint.pidfile
        )
        sys.exit(1)

    # Pidfile before the healthz bind — see services/restarter/daemon.py:run().
    if not acquire_pidfile(endpoint.pidfile, _DAEMON_MODULE, pid=os.getpid()):
        _log.info(
            "[task_maintenance] daemon already running (pidfile=%s), exiting", endpoint.pidfile
        )
        sys.exit(1)
    _log.info("[task-maintenance] pidfile written: %s", endpoint.pidfile)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    try:
        health = await start_health_server(
            "task_maintenance",
            endpoint.health_port,
            home=meiva_home(),
            liveness=liveness,
            image=image,
        )
        _log.info("[task-maintenance] healthz listening on :%s", endpoint.health_port)
        try:
            db = database()
            pool = db.pool()
            try:
                await dispatch_loop(pool, db, bus, liveness, config=config)
            finally:
                pool.close()
        finally:
            await stop_health_server(health)
    finally:
        remove_pidfile(endpoint.pidfile)
        _log.info("[task-maintenance] daemon stopped")


def main() -> None:
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    boot = ConfigBoot()
    boot.boot()
    machine = ConfiguredMachineIdentity(boot)
    assert_schema_current(boot.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(
        version=version.get, process="task_maintenance", exempt=False, end_process=hard_exit
    )
    process_home = meiva_home()

    def database() -> Database:
        return Database(
            db_config_from_boot(boot),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = telemetry.build_pipeline(
        database=database,
        machine=machine,
        home=process_home,
        now=Clock(clock_config_from_boot(boot), timers=SYSTEM_TIMERS).now,
        otlp_enabled=lambda: boot.view.observability.telemetry_otlp_enabled,
        otlp_endpoint=lambda: boot.view.observability.telemetry_otlp_endpoint,
        rng=_new_rng(),
    )
    init_gateway_process(
        name="task_maintenance",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    install_graceful_shutdown("task_maintenance")
    endpoint = task_maintenance_endpoint(boot, runtime=run_dir(process_home))
    try:
        config = read_service_config("meiva_fleet", FleetConfig, environ=boot.process_environment())
        if config is None:
            config = read_config_image(
                FleetConfig, disk_image_path("meiva_fleet", home=process_home)
            )
        asyncio.run(
            run(
                config,
                database=database,
                bus=EventBus.from_settings(rng=_new_rng(), timers=SYSTEM_TIMERS),
                image=image,
                endpoint=endpoint,
            )
        )
    except KeyboardInterrupt:
        _log.info("[task-maintenance] interrupted, shutting down")
    except Exception:
        _log.exception("[task-maintenance] daemon crashed — uncaught exception escaped run()")
        raise


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
