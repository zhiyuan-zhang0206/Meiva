# meiva:note
# Agent-host beat — the host's liveness and ownership heartbeat, and the checks that ride it.
#
# Every tick runs the permissions-helper chain check, beats the liveness probe, reaps
# crash corpses, republishes the gateway's out-of-process breaker snapshot and reports
# admission queues that outlast their live alert bound. `serve_host_lifecycle` arms the
# group that runs this loop once, before boot settlement; the turn drain joins it before
# the pools close.
#
# The chain check arrives as `require_helper_chain`: it reads the process identity, so
# `daemon.py` — this package's composition root — reads that once and passes the check
# down, exactly as it passes the admission-alert reader.
#
# It lives beside `daemon.py` rather than inside it because the loop is what the host arms
# and joins, and its wiring is testable without booting the dispatcher.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Collection

import psycopg
from psycopg_pool import PoolTimeout
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from base.agents.observation.turn_progress import TurnProgress
from base.daemon.health import Liveness
from base.deploy.progress_timeout import AGENT_HOST_BEAT_INTERVAL_S
from base.events.live.bus import EventBus
from base.log import logger

from .dispatcher import TurnScheduler
from .host import AgentHost

# meiva:note
# These lines belong to the agent host's one process log, wherever they are emitted.
host_log = logging.getLogger("services.agent_runner.agent_host.daemon")


# meiva:note
# Idle hosts still publish liveness; the same beat reaps corpses, so its step is the lattice beat.
LIVENESS_BEAT_STEP_S = AGENT_HOST_BEAT_INTERVAL_S
BEAT_DB_TIMEOUT_S = 10.0
# meiva:note
# Gateway key presence proves the 15s host loop runs; four missed beats expire it.
_TURN_PROGRESS_HEARTBEAT_TTL_S = 60
TURN_PROGRESS_PUBLISH_TIMEOUT_S = 3.0


# meiva:note
# Best-effort Redis snapshot for the gateway's out-of-process breaker; known Redis
# outages log and return False so renewal continues while defects reach the owner.
async def publish_turn_progress_heartbeat(
    bus: EventBus,
    machine: str,
    active_agents: Collection[int],
    turn_progress: TurnProgress,
    *,
    timeout_s: float = TURN_PROGRESS_PUBLISH_TIMEOUT_S,
) -> bool:
    snapshots = {
        str(agent_id): snapshot
        for agent_id in sorted(active_agents)
        if (snapshot := turn_progress.snapshot(agent_id)) is not None
    }
    try:
        async with asyncio.timeout(timeout_s):
            await bus.async_redis().set(
                f"host_turn_progress:{machine}",
                json.dumps(snapshots, separators=(",", ":")),
                ex=_TURN_PROGRESS_HEARTBEAT_TTL_S,
            )
    except TimeoutError:
        host_log.warning("[agent-host] turn-progress heartbeat publish exceeded %.1fs", timeout_s)
        return False
    except (RedisConnectionError, RedisTimeoutError, NoPermissionError, OSError):
        host_log.warning(
            "[agent-host] turn-progress heartbeat publish failed — the gateway's "
            "breaker sees stale progress until it recovers",
            exc_info=True,
        )
        return False
    return True


# meiva:note
# The database beat's failure and its retry result are structured events, not only the log line
# the host already writes (task #5767): "at 01:36, what did the beat see" has to be answerable
# from the event stream, and a line that merely mentions the words must not count. `pool` names
# the leg that failed so a future second beat (another pool, another job) is distinguishable.
# meiva:link agent/docs/loop.meiva.okf.md
def _report_database_beat_failure(
    machine: str, failures: int, error: str, *, will_exit: bool
) -> None:
    logger.warning(
        "the agent host's database beat failed {failures}/3 ({error}) — the beat reaps crash "
        "corpses and renews host ownership, so a failing beat is the first warning before the "
        "host exits",
        event="host_database_beat_failed",
        machine=machine,
        pool="host-reaper",
        failures=failures,
        limit=3,
        will_exit=will_exit,
        error=error,
    )


# meiva:note
# The other half of the same fact: a beat that failed and then succeeded says so, so an incident
# review pairs the failures with the recovery instead of reading the gap as silence.
# meiva:link agent/docs/loop.meiva.okf.md
def _report_database_beat_recovery(machine: str, failures_cleared: int) -> None:
    logger.warning(
        "the agent host's database beat recovered after {failures_cleared} consecutive failure(s)",
        event="host_database_beat_recovered",
        machine=machine,
        pool="host-reaper",
        failures_cleared=failures_cleared,
    )


# meiva:note
# Report a long queue episode without cancelling its intentionally silent turn.
def _report_long_admission_waits(
    host: AgentHost, *, read_alert_seconds: Callable[[], float]
) -> None:
    for agent_id, waited_s in host.admission.long_waiters(read_alert_seconds()):
        logger.warning(
            "hosted turn for agent {agent_id} has queued at the admission gate "
            "for {waited_s:.0f}s (limit {limit}, {queued} queued) — raise the "
            "limit or inspect the turns holding slots; the turn stays queued",
            event="host_admission_wait_exceeded",
            agent_id=agent_id,
            waited_s=round(waited_s, 1),
            limit=host.admission.limit,
            queued=host.admission.waiting,
        )


# meiva:note
# Liveness and the corpse reaper, independent of the idle dispatcher.
# beat() precedes DB work — process health must not depend on the DB.
async def beat_forever(
    liveness: Liveness,
    host: AgentHost,
    scheduler: TurnScheduler,
    machine: str,
    bus: EventBus,
    *,
    require_helper_chain: Callable[[], None],
    read_alert_seconds: Callable[[], float],
    beat_db_timeout_s: float = BEAT_DB_TIMEOUT_S,
    beat_step_s: float = LIVENESS_BEAT_STEP_S,
    publish_timeout_s: float = TURN_PROGRESS_PUBLISH_TIMEOUT_S,
) -> None:
    heartbeat_ok = True
    beat_failures = 0
    while True:
        require_helper_chain()
        liveness.beat()
        try:
            await asyncio.wait_for(host.reap_corpses(), timeout=beat_db_timeout_s)
        except (TimeoutError, psycopg.OperationalError, PoolTimeout) as error:
            beat_failures += 1
            host_log.exception("[agent-host] database beat failed (%s/3)", beat_failures)
            _report_database_beat_failure(
                machine, beat_failures, type(error).__name__, will_exit=beat_failures >= 3
            )
            if beat_failures >= 3:
                raise
        else:
            if beat_failures:
                _report_database_beat_recovery(machine, beat_failures)
            beat_failures = 0
        published = await publish_turn_progress_heartbeat(
            bus, machine, scheduler.active_agents, host.turn_progress, timeout_s=publish_timeout_s
        )
        if published and not heartbeat_ok:
            host_log.warning("[agent-host] turn-progress heartbeat publish recovered")
        heartbeat_ok = published
        _report_long_admission_waits(host, read_alert_seconds=read_alert_seconds)
        await asyncio.sleep(beat_step_s)


# meiva:note
# Propagate exhausted beat failures to the service task group.
async def ownership_beat_completion(
    liveness: Liveness,
    host: AgentHost,
    scheduler: TurnScheduler,
    machine: str,
    bus: EventBus,
    *,
    require_helper_chain: Callable[[], None],
    read_alert_seconds: Callable[[], float],
) -> Exception | None:
    await beat_forever(
        liveness,
        host,
        scheduler,
        machine,
        bus,
        require_helper_chain=require_helper_chain,
        read_alert_seconds=read_alert_seconds,
    )
    return None


async def stop_ownership_beat(beat: asyncio.Task[Exception | None] | None) -> None:
    if beat is not None:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            error = await beat
            if error is not None:
                raise error


# meiva:note
# Join the heartbeat and close its group without wrapping its retained error.
async def close_ownership_beat(
    beat: asyncio.Task[Exception | None] | None, tasks: asyncio.TaskGroup | None
) -> None:
    try:
        await stop_ownership_beat(beat)
    finally:
        if tasks is not None:
            await tasks.__aexit__(None, None, None)
