"""Heartbeat daemon — gateway-owned idle-agent check-in dispatcher.

It sends due, unpaused check-ins to idle native agents and active impersonators.
Native clocks start at the last completed turn; impersonation clocks run from
lease activation regardless of external activity. Each check-in is a durable
`heartbeat` inbound. The inbound-insert trigger wakes the agent anywhere in the
cluster. It runs once per cluster on the gateway and never touches local sessions.

Dispatch is paced for fleet scale: rather than checking in on every due agent in
one batch per idle window (which would wake 100-300 idle agents simultaneously —
a decompression + LLM thundering herd, self-perpetuating because the check-in
resets each agent's idle clock in lockstep), it polls on a fine cadence (min of
`MEIVA_HEARTBEAT_INTERVAL_SECONDS` and `_DISPATCH_STEP_S`), de-phases due-times with a
deterministic per-agent jitter, and caps check-ins per step for a hard global
wake-rate ceiling. See the "Wakeup-storm flattening" note below.

Usage: .venv/bin/python -m services.wake.heartbeat.daemon. Kept alive by the root
supervisor through the roster's `/healthz` probe; repeated failed rounds end the process.
"""

import asyncio
import logging
import math
import os
import random
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.clock import Clock, Timers
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot, settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.host.exit_codes import hard_exit as _hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from ops.cluster import rpc as cluster_rpc
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.wake.heartbeat import JITTER_SPAN_S, completion_digest
from services.wake.heartbeat.liveness import LivenessConfig, run_liveness_pass
from services.wake.heartbeat.loops import DispatchRound, LivenessRound, dispatch_loop, liveness_loop
from services.wake.heartbeat.selection import select_idle_agents_needing_heartbeat

_log = logging.getLogger("services.wake.heartbeat.daemon")


# meiva:note
# The process's random generator, built once at its entry for the event bus's retry jitter. Jitter
# is not a secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("heartbeat")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


# Liveness staleness ceiling. The loop sleeps a long inter-poll interval (default
# 300s), so `sleep_with_liveness` beats every _LIVENESS_BEAT_STEP_S during that
# wait; the ceiling only has to exceed that step, not the whole interval. A
# genuinely wedged loop (a beat step that never returns) still trips /healthz 503
# -> watchdog respawn.
_LIVENESS_TIMEOUT_S = 60.0
# One digest round is a bounded read plus one delivery per completed agent-hour; the
# loop also beats during its 60 s waits.
_DIGEST_LIVENESS_TIMEOUT_S = 300.0

# ── Wakeup-storm flattening (density hardening) ──
# A check-in wakes an idle agent: its ~90MB compressed heap decompresses (~25-40ms
# of CPU on Apple Silicon) and it opens an LLM turn. Firing every due agent in one
# tight loop therefore triggers a simultaneous decompression + LLM burst; worse,
# the woken agent runs a turn, which resets its `last_active_at` (the idle clock),
# so a batch woken together comes due together next cycle — the fleet
# self-synchronizes into a
# recurring spike. Three coupled defenses keep the wake rate bounded at fleet
# scale (100-300 idle agents):
#   * poll on a fine cadence (_DISPATCH_STEP_S), not once per idle window, so due
#     agents are noticed in small time-slices instead of one 300s batch;
#   * de-phase each agent's due-time by a deterministic per-agent jitter
#     (JITTER_SPAN_S) so a fleet that went idle together does not come due
#     together — this breaks the self-synchronization;
#   * cap check-ins per dispatch step (_MAX_CHECKINS_PER_STEP) for a hard global
#     wake-rate ceiling (~_MAX_CHECKINS_PER_STEP / _DISPATCH_STEP_S per second),
#     draining even a fully-synchronized backlog (e.g. after a mass restart)
#     smoothly instead of in one instantaneous spike.
# Steady-state at 300 idle agents (idle_threshold 300s) the natural due-rate is
# ~1/s, under the ~1.7/s ceiling, so jitter alone carries it; the cap only bites
# on a synchronized burst. Trade-off: a fully-synchronized backlog's tail agents
# wait longer for their first check-in (backlog / ceiling seconds) — acceptable
# for a liveness check-in that is a safety net, not a latency-sensitive signal.
_DISPATCH_STEP_S = 15.0
# JITTER_SPAN_S / STALE_PENDING_S live in `services.wake.heartbeat` — the inspector's
# projection mirrors them, so there is one drift-free source.
_MAX_CHECKINS_PER_STEP = 25

# Consecutive-failed-check-in backoff (Task #1928): a check-in that produces no
# LLM turn is a failed check-in — the agent is wedged (the 3962 context-overflow
# case: 1146 nudges against a permanently-rejecting provider, evidence #1289)
# and every nudge just re-fires the doomed call. The daemon tracks the streak
# per agent in-process and spaces the next check-in by `2^streak` idle windows,
# so a broken agent stops being poked on the normal cadence while staying
# recoverable: the first check-in after the backoff window is a probe, and a
# real turn (last_active_at advancing, or fresh activity from a real wake)
# resets the streak. In-process state only: a daemon restart re-probes at the
# normal cadence, and the agent-side circuit breaker (open = check-ins consumed
# without an LLM call) keeps the doomed calls off meanwhile.
# streak=1 -> next check-in after 2 idle windows; the cap bounds the longest
# silence (~5.3h at a 5min idle threshold).
BACKOFF_MAX_WINDOWS = 64
# meiva:note
# Platform-side nudge backoff (B7): a no-op nudge — no real inbound arrived and the agent did
# not pause — raises the agent's persisted backoff level, which stretches the reminder floor to
# heartbeat_interval * 2^level, capped at MEIVA_HEARTBEAT_BACKOFF_MAX_INTERVAL_SECONDS (a day by
# default; the inspector projection mirrors the same ceiling as its cadence value). The level
# lives in agents_meta.heartbeat_backoff_level (survives daemon restarts); the consecutive-no-op
# counter is in-process only, so a restart recounts from zero. Real inbound or a pause resets
# the level to 0.
# meiva:link services/docs/gateway_side/heartbeat.meiva.okf.md
_BACKOFF_MAX_LEVEL = 16  # schema CHECK bound; the raise-time cap is tighter


# meiva:note
# The highest backoff level whose stretched interval still stays under the configured ceiling;
# past it the selection SQL's LEAST clamp is what holds the floor.
# meiva:link services/docs/gateway_side/heartbeat.meiva.okf.md
def backoff_max_level(interval_s: float) -> int:
    if interval_s <= 0:
        return 0
    ceiling_s = settings.daemon.heartbeat_backoff_max_interval_seconds
    return max(0, math.floor(math.log2(ceiling_s / interval_s)))


# An idle_minutes reading this much below the value recorded at check-in time
# counts as "the check-in produced a turn" (slack absorbs clock skew).
_ADVANCE_SLACK_MINUTES = 0.5

# Heartbeat note delivered as a system note (kind='heartbeat') so the
# agent sees it as a framework-level marker, not an ordinary chat message.
# The claim node wraps it via system_note_message(tag=NoteTag.HEARTBEAT).


def send_heartbeat_checkin(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    idle_minutes: float,
    *,
    reason: str | None = None,
) -> None:
    """INSERT one `heartbeat` inbound for `agent_id`, then publish a Redis wake so
    the (idle-by-selection) target picks it up now instead of at its next inbound-
    wait SELECT recheck. Delivered as a system note (kind='heartbeat') — the claim
    node wraps it via system_note_message(tag=NoteTag.HEARTBEAT). `idle_minutes`
    rides the telemetry event only; the inbound content is the plain check-in."""
    content = (
        "Heartbeat. Review your existing responsibilities and continue actionable work. "
        "For a known wait, pause your heartbeat; if your role is complete, end it."
    )
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'heartbeat', 'system')",
            (agent_id, content),
        )
        # This must share the inbound transaction: once the inbound is
        # committed, a consumed no-LLM heartbeat still has a durable cadence
        # floor even after a daemon restart loses its in-memory backoff state.
        cur.execute("UPDATE agents_meta SET last_heartbeat_at = now() WHERE id = %s", (agent_id,))
        # The event name 'heartbeat_nudged' is stored row data — renaming it
        # would strand the existing history. Emit through the unified pipeline
        # (`base/telemetry/emitter.py`).
        #
        # ts time-source note: the emitter stamps datetime.now(UTC) at ENQUEUE
        # time (process clock, one time source for the whole stream), so it can
        # trail the inbound's transaction time by the drain interval (≤0.5s)
        # plus any queue backlog.
        # meiva:note
        # A check-in that exists because a held-path settle failed is a different fact
        # from a routine nudge: it costs a model turn that nothing else would have spent,
        # so it is counted under its own event with the reason attached (#5468).
        # meiva:link docs/conventions/agents/agent-impersonation.md
        telemetry.emit(
            "telemetry",
            "impersonation_handoff_checkin" if reason else "heartbeat_nudged",
            level="info",
            agent_id=agent_id,
            source="system",
            attributes={"idle_minutes": round(idle_minutes), "reason": reason}
            if reason
            else {"idle_minutes": round(idle_minutes)},
        )
    # The inbound is committed on `with` exit (the emit above is enqueued and
    # lands on the emitter's next batch — best-effort, JSONL-mirrored). The wake
    # is published after the inbound row is durable. Best-effort wake (see
    # base.agents.impersonation.wake.publish_inbound_wake); heartbeat carries no user-facing inbound
    # id, so "0".
    publish_inbound_wake(db, bus, agent_id, "0")


def _reconcile_agent(
    pool: ConnectionPool,
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    pending_checkin: dict[int, float],
    failure_streak: dict[int, int],
    noop_streak: dict[int, int],
    idle_threshold_s: float,
    heartbeat_interval_s: float,
    threshold: int,
) -> None:
    """Judge one tracked agent's last check-in: update its failure and no-op streaks."""
    cur.execute(
        "SELECT status, "
        "(EXTRACT(EPOCH FROM (now() - last_active_at)) / 60.0)::double precision, "
        "heartbeat_backoff_level, "
        "(heartbeat_paused_until IS NOT NULL AND heartbeat_paused_until > now()), "
        "(last_heartbeat_at IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM inbound_messages im "
        "  WHERE im.agent_id = agents_meta.id AND im.kind <> 'heartbeat' "
        "    AND im.created_at > agents_meta.last_heartbeat_at)) "
        ", EXISTS (SELECT 1 FROM agent_impersonations lease "
        "WHERE lease.agent_id=agents_meta.id AND lease.status='active') "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    )
    row = cur.fetchone()
    sent_at = pending_checkin.pop(agent_id, None)
    if row is None or row[0] not in ("idling", "running") or bool(row[5]):
        # External progress is not measured by native turns; never penalize it.
        failure_streak.pop(agent_id, None)
        noop_streak.pop(agent_id, None)
        return
    idle_minutes = row[1]
    advanced = (
        sent_at is not None
        and idle_minutes is not None
        and idle_minutes < sent_at - _ADVANCE_SLACK_MINUTES
    )
    recovered = advanced or (idle_minutes is not None and idle_minutes < idle_threshold_s / 60.0)
    if recovered:
        failure_streak.pop(agent_id, None)
    elif sent_at is not None:
        failure_streak[agent_id] = failure_streak.get(agent_id, 0) + 1
    # Not pending and not recovered: keep the existing streak and
    # its previously assigned backoff deadline.

    # B7 no-op nudge streak — independent of the failure streak above.
    if bool(row[3]) or bool(row[4]):
        noop_streak.pop(agent_id, None)
    elif sent_at is not None:
        _count_noop_nudge(
            pool, agent_id, int(row[2] or 0), noop_streak, heartbeat_interval_s, threshold
        )


def _count_noop_nudge(
    pool: ConnectionPool,
    agent_id: int,
    level: int,
    noop_streak: dict[int, int],
    heartbeat_interval_s: float,
    threshold: int,
) -> None:
    """A check-in with no real inbound and no pause: count it, and at `threshold` raise the level."""
    noop_streak[agent_id] = noop_streak.get(agent_id, 0) + 1
    if noop_streak[agent_id] >= threshold:
        noop_streak[agent_id] = 0
        new_level = min(level + 1, backoff_max_level(heartbeat_interval_s))
        if new_level > level:
            _raise_backoff_level(pool, agent_id, new_level, heartbeat_interval_s)


def reconcile_checkin_outcomes(
    pool: ConnectionPool,
    *,
    pending_checkin: dict[int, float],
    failure_streak: dict[int, int],
    idle_threshold_s: float,
    noop_streak: dict[int, int] | None = None,
    heartbeat_interval_s: float = 300.0,
    noop_nudges_threshold: int | None = None,
) -> None:
    """Judge the previous cycle's check-ins and detect recovery, updating the
    per-agent failure streak and the B7 no-op-nudge streak.

    For every tracked agent (a check-in sent last cycle, or already on a
    streak):

    - the check-in advanced `last_active_at` (the agent ran a real turn) →
      streak reset;
    - `last_active_at` is now fresh (a real wake produced a turn without this
      daemon's nudge — the agent recovered on its own) → streak reset;
    - a sent check-in produced no turn → streak += 1 (the next check-in is
      spaced by `2^streak` idle windows);
    - the agent left the daemon's lanes (terminated / missing) → stop tracking.

    B7 (platform-side nudge backoff) rides the same pass: a sent check-in that
    produced neither a real inbound nor an agent pause increments `noop_streak`;
    at `noop_nudges_threshold` consecutive no-ops the persisted
    `heartbeat_backoff_level` is raised by one (the reminder floor stretches to
    `heartbeat_interval * 2^level`, capped at 24h) and the counter restarts. A
    real inbound or a pause clears the streak (the level reset itself is the
    `_sweep_backoff_resets` pass — it also covers agents this daemon is not
    tracking).

    `pending_checkin` maps agent_id → the `idle_minutes` observed when its
    check-in was sent; comparing `idle_minutes` readings (both derived from the
    DB clock) avoids wall-clock drift between this process and Postgres.
    """
    noop_streak = {} if noop_streak is None else noop_streak
    threshold = (
        settings.daemon.heartbeat_backoff_consecutive_noop_nudges
        if noop_nudges_threshold is None
        else noop_nudges_threshold
    )
    tracked = set(pending_checkin) | set(failure_streak) | set(noop_streak)
    if not tracked:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for agent_id in tracked:
            _reconcile_agent(
                pool,
                cur,
                agent_id,
                pending_checkin=pending_checkin,
                failure_streak=failure_streak,
                noop_streak=noop_streak,
                idle_threshold_s=idle_threshold_s,
                heartbeat_interval_s=heartbeat_interval_s,
                threshold=threshold,
            )


def _raise_backoff_level(
    pool: ConnectionPool, agent_id: int, new_level: int, interval_s: float
) -> None:
    """Persist a raised B7 backoff level and emit its event."""
    stretched_s = int(
        min(
            interval_s * (2**new_level),
            settings.daemon.heartbeat_backoff_max_interval_seconds,
        )
    )
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET heartbeat_backoff_level = %s WHERE id = %s",
            (new_level, agent_id),
        )
        telemetry.emit(
            "telemetry",
            "heartbeat_backoff_raised",
            level="info",
            agent_id=agent_id,
            source="system",
            attributes={
                "level": new_level,
                "interval_seconds": stretched_s,
            },
        )


def sweep_backoff_resets(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, heartbeat_backoff_level, "
            "(heartbeat_paused_until IS NOT NULL AND heartbeat_paused_until > now()), "
            "(last_heartbeat_at IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id AND im.kind <> 'heartbeat' "
            "    AND im.created_at > agents_meta.last_heartbeat_at)) "
            "FROM agents_meta WHERE heartbeat_backoff_level > 0"
        )
        rows = cur.fetchall()
    for agent_id, level, paused, real_inbound in rows:
        if not paused and not real_inbound:
            continue
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_backoff_level = 0 WHERE id = %s",
                (agent_id,),
            )
            telemetry.emit(
                "telemetry",
                "heartbeat_backoff_reset",
                level="info",
                agent_id=agent_id,
                source="system",
                attributes={
                    "previous_level": int(level),
                    "reason": "paused" if paused else "real_inbound",
                },
            )


def backoff_deadlines(
    failure_streak: dict[int, int],
    idle_threshold_s: float,
    deadline_state: dict[int, tuple[int, float]] | None = None,
    *,
    now: float,
) -> dict[int, float]:
    """Keep each absolute deadline until its agent's failure streak changes.

    A new streak gets `now + min(2^streak, BACKOFF_MAX_WINDOWS) *
    idle_threshold`. A streak of 1 doubles the normal interval; the cap bounds
    the longest silence (~5.3h at a 5min threshold) so the daemon still probes
    a wedged agent occasionally. The state is in-process only.
    """
    deadline_state = {} if deadline_state is None else deadline_state
    for agent_id in deadline_state.keys() - failure_streak.keys():
        del deadline_state[agent_id]
    for agent_id, streak in failure_streak.items():
        previous = deadline_state.get(agent_id)
        if previous is None or previous[0] != streak:
            deadline_state[agent_id] = (
                streak,
                now + min(2**streak, BACKOFF_MAX_WINDOWS) * idle_threshold_s,
            )
    return {agent_id: deadline for agent_id, (_, deadline) in deadline_state.items()}


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(
        _pidfile(config, runtime=runtime), "services.wake.heartbeat.daemon", pid=os.getpid()
    ):
        _log.info(
            "[heartbeat] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def _remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    remove_pidfile(_pidfile(config, runtime=runtime))


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(config, runtime=runtime), "services.wake.heartbeat.daemon")


@dataclass(frozen=True)
class DispatchConfig:
    idle_threshold_s: float
    heartbeat_interval_s: float
    backoff_max_interval_s: float


# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md
class HeartbeatDispatch:
    def __init__(
        self,
        pool: ConnectionPool,
        db: Database,
        bus: EventBus,
        *,
        config: DispatchConfig,
        clock: Clock,
    ) -> None:
        self._pool = pool
        self._db = db
        self._bus = bus
        self._config = config
        self._clock = clock
        self._pending_checkin: dict[int, float] = {}
        self._failure_streak: dict[int, int] = {}
        self._deadline_state: dict[int, tuple[int, float]] = {}
        self._noop_streak: dict[int, int] = {}
        _log.info(
            "[heartbeat] daemon started, step=%.0fs, idle_threshold=%.0fs, "
            "jitter_span=%.0fs, max_checkins_per_step=%d (wake-rate ceiling ~%.2f/s)",
            self.interval_s,
            config.idle_threshold_s,
            JITTER_SPAN_S,
            _MAX_CHECKINS_PER_STEP,
            _MAX_CHECKINS_PER_STEP / self.interval_s,
        )

    @property
    def interval_s(self) -> float:
        return min(self._config.heartbeat_interval_s, _DISPATCH_STEP_S)

    def send_checkin(self, agent_id: int, idle_minutes: float, reason: str | None = None) -> None:
        send_heartbeat_checkin(
            self._pool, self._db, self._bus, agent_id, idle_minutes, reason=reason
        )

    def run_round(self) -> None:
        sweep_backoff_resets(self._pool)
        reconcile_checkin_outcomes(
            self._pool,
            pending_checkin=self._pending_checkin,
            failure_streak=self._failure_streak,
            idle_threshold_s=self._config.idle_threshold_s,
            noop_streak=self._noop_streak,
            heartbeat_interval_s=self._config.heartbeat_interval_s,
        )
        now = self._clock.now().timestamp()
        rows = select_idle_agents_needing_heartbeat(
            self._pool,
            self._config.idle_threshold_s,
            now=now,
            heartbeat_interval_s=self._config.heartbeat_interval_s,
            backoff_max_interval_s=self._config.backoff_max_interval_s,
            jitter_span_s=JITTER_SPAN_S,
            limit=_MAX_CHECKINS_PER_STEP,
            backoff_until=backoff_deadlines(
                self._failure_streak,
                self._config.idle_threshold_s,
                self._deadline_state,
                now=now,
            ),
        )
        for agent_id, idle_minutes, reason in rows:
            try:
                self.send_checkin(agent_id, idle_minutes, reason)
                self._pending_checkin[agent_id] = idle_minutes
                _log.info(
                    "[heartbeat] checked in on idle agent %s (idle %.0f min)",
                    agent_id,
                    idle_minutes,
                )
            except Exception as exc:
                _log.error("[heartbeat] check-in for agent %s failed: %r", agent_id, exc)


class AgentLivenessRound:
    def __init__(
        self, db: Database, pool: ConnectionPool, bus: EventBus, rpc: cluster_rpc.RpcClient
    ) -> None:
        self._db = db
        self._pool = pool
        self._bus = bus
        self._rpc = rpc

    async def run_round(self) -> None:
        await run_liveness_pass(
            self._db,
            self._pool,
            self._bus,
            probe=partial(self._rpc.dispatch_to_machine, self._db),
            config=LivenessConfig(settings.gateway.status_probe_timeout_seconds),
        )


async def run_loops(
    dispatcher: DispatchRound,
    liveness_round: LivenessRound,
    digest_round: completion_digest.DigestRound,
    liveness: LivenessGroup,
    *,
    timers: Timers,
) -> None:
    dispatch_progress = liveness.register("dispatch", _LIVENESS_TIMEOUT_S)
    liveness_progress = liveness.register("liveness", _LIVENESS_TIMEOUT_S)
    digest_progress = liveness.register("completion_digest", _DIGEST_LIVENESS_TIMEOUT_S)
    async with asyncio.TaskGroup() as loops:
        loops.create_task(dispatch_loop(dispatcher, dispatch_progress, timers=timers))
        loops.create_task(liveness_loop(liveness_round, liveness_progress, timers=timers))
        loops.create_task(completion_digest.completion_digest_loop(digest_round, digest_progress))


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
    machine: MachineIdentityReader,
    clock: Clock,
) -> None:
    """Start the daemon: healthz server -> write pidfile -> connect DB -> enter main loop."""
    if _is_running(process_config, runtime=runtime):
        _log.info(
            "[heartbeat] daemon already running (pidfile=%s), exiting",
            _pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    write_pidfile(process_config, runtime=runtime)
    _log.info("[heartbeat] pidfile written: %s", _pidfile(process_config, runtime=runtime))

    liveness = LivenessGroup(timers=clock)
    endpoint = _endpoint(process_config, runtime=runtime)
    health = await start_health_server(
        "heartbeat", endpoint.health_port, liveness=liveness, image=image
    )
    _log.info("[heartbeat] healthz listening on :%s", endpoint.health_port)

    db = database()
    pool = db.pool()
    bus = EventBus.from_settings(rng=_new_rng(), timers=clock)
    try:
        # One TaskGroup owns the resident loops, each with its own progress tracker
        # so a stalled loop cannot be masked by a busy sibling. The liveness pass
        # (Task #1174) is a slow independent loop beside the check-in loop, so a
        # stalled probe fan-out (bounded by _PROBE_TIMEOUT_S) can never delay a
        # check-in; the completion digest is a third. A loop that raises cancels
        # its siblings and ends the process, and the supervisor restarts it.
        dispatcher = HeartbeatDispatch(
            pool,
            db,
            bus,
            config=DispatchConfig(
                settings.daemon.heartbeat_idle_threshold_seconds,
                settings.daemon.heartbeat_interval_seconds,
                settings.daemon.heartbeat_backoff_max_interval_seconds,
            ),
            clock=clock,
        )
        rpc = cluster_rpc.RpcClient.from_boot(process_config, timers=clock, rng=_new_rng())
        liveness_round = AgentLivenessRound(db, pool, bus, rpc)
        digest_round = completion_digest.CompletionDigestRound(
            pool, db, bus, machine, clock, completion_digest.DigestConfig(), rpc
        )
        await run_loops(
            dispatcher,
            liveness_round,
            digest_round,
            liveness,
            timers=clock,
        )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile(process_config, runtime=runtime)
        _log.info("[heartbeat] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `meiva stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = ConfiguredMachineIdentity(machine_config)
    process_home = meiva_home().resolve()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(machine_config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="heartbeat")

    def database() -> Database:
        return Database(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="heartbeat",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    install_graceful_shutdown("heartbeat")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — a stranded-hold grading pass among them — and a stop signal
    # must never wait on those (see `_hard_exit`). The runner is therefore
    # never closed: after the explicit drain below, teardown is skipped by the
    # hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                process_config=machine_config,
                runtime=process_home / "run",
                database=database,
                image=image,
                machine=machine,
                clock=Clock.from_settings(),
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[heartbeat] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still cancels the liveness loop,
        # closes the pool and stops the health server. The executor is
        # deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[heartbeat] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[heartbeat] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile(machine_config, runtime=process_home / "run")
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
