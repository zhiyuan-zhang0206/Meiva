import asyncio
import logging
import os
import random
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.api_contracts.agent_contract import AgentStatus
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
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugins.config_registration import (
    disk_image_path,
    read_config_image,
    read_service_config,
)
from base.paths import meiva_home
from base.telemetry.process_logging import init_gateway_process
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from meiva_builtins.plugins.meiva_fleet.task_maintenance import routing
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

# meiva:note
# Task-maintenance daemon — gateway-owned task reminders + escalation, one loop
# cluster-wide, registered into the ops service roster by the `meiva_fleet` plugin
# rather than hardcoded in `services/supervision/roster/spec.py`. Reminders and
# escalations insert into `inbound_messages` directly and then best-effort wake via
# Redis, modeled on heartbeat; counter updates stay direct DB writes. They never
# resurrect a terminated agent and never address one either: a dead owner's due
# reminder is rerouted to its manager, or into the human queue when no live agent
# owns an ancestor task (user ruling 2026-10-25 — a dead owner's queue had held the
# reminders nobody read). The escalate pass fires at `reminder_count ==
# task_escalate_n`, reaches the same manager, and `escalated_at` keeps the
# delegator leg at most once per overdue window; a write that changes the work clears
# `escalated_at` with the reminder counters and re-arms it, while a note-only write does
# not (the criterion is `base/agents/tasks/reminder_window.py`). A schema-drift exit
# (`psycopg.ProgrammingError`) is revived by the root health monitor's `/healthz`
# probe on the next round instead of staying dead.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md
_log = logging.getLogger("meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon")


_DAEMON_MODULE = "meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon"


# meiva:note
# The process's random generator, built once at its entry for the event bus's retry jitter. Jitter
# is not a secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


def task_maintenance_endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("task_maintenance")


_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 30.0


# ── Reminder pass ──────────────────────────────────────────────────────────────

# meiva:note
# Tasks whose remind_interval_seconds has elapsed AND the owner hasn't been reminded
# within this overdue window (last_reminded_at gates it within
# max(backoff, remind_interval_seconds) — each task repeats at its own cadence).
# A task that still has an open direct child is a container, not a work item: its
# children carry the reminders, and repeating them on the parent adds nothing.
# A terminated owner stays eligible here: its due reminder is rerouted rather
# than dropped, and the row lock is what stops a concurrent sweep from accepting
# the same cadence twice. `FOR NO KEY UPDATE` (not `FOR UPDATE`) keeps that lock
# from blocking a gateway notice insert that references the task by FK — the same
# mode the escalate pass uses for the same reason. The join reads the owner's status without locking the
# identity row — notices and messages take that lock themselves.
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
      AND NOT EXISTS (
          SELECT 1 FROM agent_tasks child
          WHERE child.parent_id = t.id AND child.status = 'in_progress'
      )
      AND (
          t.last_reminded_at IS NULL
          OR now() - t.last_reminded_at
             > make_interval(secs => GREATEST(%s, t.remind_interval_seconds))
      )
    ORDER BY t.priority, t.id
"""


@dataclass(frozen=True)
class _DueTask:
    id: int
    owner: int
    title: str
    remind_interval_seconds: int
    elapsed_s: int
    priority: str


# meiva:note
# One manager's digest (grouped by the terminated owner that held the tasks) and one
# human notice, both accepted in the sweep's transaction and announced after commit.
@dataclass(frozen=True)
class _Reroute:
    manager: int
    by_owner: Mapping[int, tuple[_DueTask, ...]]
    inbound_id: int
    content: str


@dataclass(frozen=True)
class _HumanNotice:
    owner: int
    tasks: Sequence[_DueTask]
    notification_id: int | None


# meiva:note
# A reminder is a task system notification (NoteTag 'task'), never peer chat (user
# ruling 2026-08-27), and its payload forbids a wake from resurrecting the owner.
def _task_note_payload() -> dict[str, object]:
    return {"note_tag": "task", "task_notification": True, "delivery_resurrect": False}


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


def _stranded_notice_title(owner: int, tasks: list[_DueTask]) -> str:
    return (
        f"Agent #{owner} is terminated — {len(tasks)} task(s) still in progress "
        "and due for a reminder"
    )


def _stranded_notice_content(owner: int, tasks: list[_DueTask]) -> str:
    lines = [
        f"Agent #{owner} was terminated while these tasks were open, and no live agent owns "
        "any of their ancestor tasks, so nothing else is being reminded. Reassign each task "
        "to a live agent, cancel it, or reply with what should happen:"
    ]
    lines.extend(_task_line(task) for task in tasks)
    return "\n".join(lines)


def _accept_owner_digests(
    cur: psycopg.Cursor, live: dict[int, list[_DueTask]]
) -> list[tuple[int, int, str, list[_DueTask]]]:
    accepted: list[tuple[int, int, str, list[_DueTask]]] = []
    for owner, tasks in sorted(live.items()):
        content = _reminder_digest_message(tasks)
        inbound_id, _ = insert_inbound_message_in_transaction(
            cur, owner, content, "system", kind="system_note", payload=_task_note_payload()
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
        cur, manager, content, "system", kind="system_note", payload=_task_note_payload()
    )
    for tasks in by_owner.values():
        for task in tasks:
            advance_reminder_counters(cur, task.id, delegated=True)
    return _Reroute(
        manager, {owner: tuple(tasks) for owner, tasks in by_owner.items()}, inbound_id, content
    )


def _accept_human_notice(
    cur: psycopg.Cursor, conn: psycopg.Connection, owner: int, tasks: list[_DueTask]
) -> _HumanNotice | None:
    staged = routing.stage_human_notice(
        cur,
        conn,
        agent_id=owner,
        task_id=None,
        title=_stranded_notice_title(owner, tasks),
        content=_stranded_notice_content(owner, tasks),
        priority=min(task.priority for task in tasks),
    )
    for task in tasks:
        advance_reminder_counters(cur, task.id)
    if staged is None:
        return None
    return _HumanNotice(owner, tasks, staged[1])


def _accept_reroutes(
    cur: psycopg.Cursor, conn: psycopg.Connection, orphaned: dict[int, list[_DueTask]]
) -> tuple[list[_Reroute], list[_HumanNotice]]:
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
    notices: list[_HumanNotice] = []
    # meiva:note
    # The human leg writes `agent_notices`, whose local id is allocated per agent: the
    # identity lock it takes here is the same physical lock the gateway's notice creator
    # holds, in the same order (task rows, then identities), so two writers cannot pick
    # the same local id and no notice is staged for an agent mid-termination.
    if stranded:
        cur.execute(
            "SELECT id FROM agents WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            (sorted(stranded),),
        )
    for owner, tasks in sorted(stranded.items()):
        notice = _accept_human_notice(cur, conn, owner, tasks)
        if notice is not None:
            notices.append(notice)
    return reroutes, notices


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


def _announce_human_notices(
    notices: list[_HumanNotice], pool: ConnectionPool, bus: EventBus
) -> None:
    from base.events.notifications.publish import announce_sync

    for notice in notices:
        if notice.notification_id is not None:
            announce_sync(pool, bus, notice.notification_id)
        publish_agent_updated_sync(bus, notice.owner)
        telemetry.emit(
            "telemetry",
            "task_reminder_rerouted",
            agent_id=notice.owner,
            source="system",
            attributes={
                "owner_id": notice.owner,
                "leg": "user",
                "recipient": None,
                "task_count": len(notice.tasks),
                "task_ids": [task.id for task in notice.tasks],
            },
        )


# meiva:note
# Delivery follows the owner's status. A live owner receives the check-in digest. A
# terminated owner cannot act, so each of its due tasks is rerouted to the manager
# `routing.nearest_manager` names — one digest per manager, grouped by the dead owner
# — or, when no ancestor has a live owner, into the human queue as one notice on the
# dead owner. Both reroute legs advance the cadence like any other reminder, so the
# next one waits its own window instead of repeating on every 5-minute sweep.
def run_reminders(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    backoff_seconds: float,
) -> int:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(_REMINDER_SQL + " FOR NO KEY UPDATE OF t SKIP LOCKED", (backoff_seconds,))
        live: dict[int, list[_DueTask]] = defaultdict(list)
        orphaned: dict[int, list[_DueTask]] = defaultdict(list)
        for task_id, owner, title, interval, elapsed, priority, status in cur.fetchall():
            task = _DueTask(task_id, owner, title, interval, elapsed, priority)
            (orphaned if status == AgentStatus.TERMINATED else live)[owner].append(task)
        digests = _accept_owner_digests(cur, live)
        reroutes, notices = _accept_reroutes(cur, conn, orphaned)
    _announce_owner_digests(digests, db, bus)
    _announce_reroutes(reroutes, db, bus)
    _announce_human_notices(notices, pool, bus)
    if reroutes or notices:
        _log.info(
            "[task-maintenance] rerouted %d manager digest(s) and %d human notice(s)",
            len(reroutes),
            len(notices),
        )
    return len(digests) + len(reroutes) + len(notices)


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


def run_escalate(pool: ConnectionPool, db: Database, bus: EventBus, escalate_n: int) -> int:
    """Accept current escalation state atomically, then announce committed work."""
    from base.events.notifications.publish import announce_sync
    from meiva_builtins.plugins.meiva_fleet.task_maintenance.escalation import accept_escalations

    receipts = accept_escalations(pool, escalate_n)
    for receipt in receipts:
        if receipt.notification_id is not None:
            # meiva:note
            # The escalation transaction staged the user leg's delivery next to
            # the notice, so announcing it here can only mark what the commit
            # already promised: the same category, dedupe and live event a
            # gateway-posted notice gets. Without it, an escalation meant to
            # reach a human reached only the console inbox.
            # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
            announce_sync(pool, bus, receipt.notification_id)
        if receipt.inbound_id is not None:
            announce_reminder(db, bus, receipt.recipient, receipt.inbound_id, receipt.content)
        else:
            publish_agent_updated_sync(bus, receipt.recipient)
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
        _log.info("[task-maintenance] accepted %d escalation digests/notices", len(receipts))
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
            run_reminders(pool, db, bus, backoff_seconds)
            run_escalate(pool, db, bus, escalate_n)
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
    if not acquire_pidfile(endpoint.pidfile, _DAEMON_MODULE):
        _log.info(
            "[task_maintenance] daemon already running (pidfile=%s), exiting", endpoint.pidfile
        )
        sys.exit(1)
    _log.info("[task-maintenance] pidfile written: %s", endpoint.pidfile)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    try:
        health = await start_health_server(
            "task_maintenance", endpoint.health_port, liveness=liveness, image=image
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
    gate = ProcessDbGate(version=version.get, process="task_maintenance", exempt=False)
    process_home = meiva_home()

    def database() -> Database:
        return Database(
            db_config_from_boot(boot),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = telemetry.build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="task_maintenance",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    install_graceful_shutdown("task_maintenance")
    endpoint = task_maintenance_endpoint()
    try:
        config = read_service_config("meiva_fleet", FleetConfig)
        if config is None:
            config = read_config_image(
                FleetConfig, disk_image_path("meiva_fleet", home=process_home)
            )
        asyncio.run(
            run(
                config,
                database=database,
                bus=EventBus.from_settings(rng=_new_rng()),
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
