"""Per-agent cached runtime state for the hosted agent runner, plus the
wake-time admission of an agent's stored model configuration."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agent.process_boot import boot_agent_scope
from base.agents.inbox.harness_notice import insert_harness_notice
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentBrain, AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.factory import validate_model_launch
from base.lm.harness import UnsolicitedOutput, UnsolicitedSink
from base.lm.harness.bridge import HarnessBridge
from base.lm.harness.engine import TurnEngine
from base.lm.harness.handoff import HarnessContribution
from base.lm.harness.registration import RegistrationStore, SecureRandom
from base.lm.harness.registry import SessionRegistry
from base.lm.harness.wiring import BridgeRootValues, bridge_for
from base.lm.registry import resolve_available_model
from base.log import logger
from base.paths import workspace_dir
from services.agent_runner.agent_host import TurnOutcome as TurnOutcome
from services.agent_runner.agent_host.scheduling import stored_config

__all__ = [
    "AgentRuntime",
    "HostCachePolicy",
    "HostPolicy",
    "HostStats",
    "StallTiming",
    "TurnOutcome",
    "admit_stored_model",
]


# meiva:note
# The bounds of the no-progress stall guard around one graph invocation: how often it looks at
# the turn-progress clock, how long a silent turn is tolerated, and how long a cancelled
# invocation has to unwind before the host must restart.
@dataclass(frozen=True)
class StallTiming:
    scan_interval_s: float
    no_progress_timeout_s: float
    unwind_timeout_s: float


# meiva:note
# The runtime cache's bounds: how long an unused runtime stays warm and how many are kept.
@dataclass(frozen=True)
class HostCachePolicy:
    idle_ttl_seconds: float
    size: int


# meiva:note
# What one host is configured with, read once by the composition root. Capacity, the cache
# limits, the model defaults, the registration lifetime and the stall bounds are values; a
# change to the cluster configuration reaches the host with its next start. `default_reader`
# and `clock_factory` are the two legacy readers the base context and slice APIs still take.
# Handoff stamping is a separate AgentHost resource and mints a pure policy for each use;
# this remaining reader-based host policy is not a pure configuration value.
#
# `provider_keys` is built by the daemon at boot, after the bootstrap delivered the keys to its
# environment.
@dataclass(frozen=True)
class HostPolicy:
    max_concurrent_turns: int
    provider_keys: ProviderKeys
    cache: HostCachePolicy
    default_model: str
    llm_override: str
    default_reader: Callable[[str, str], Any]
    clock_factory: Callable[[], Clock]
    harness_registration_ttl_seconds: float
    stall: StallTiming
    # meiva:note
    # The budget this host's takeover wake may spend settling its owed handoff write, read from
    # `MEIVA_HOST_HELD_SETTLE_BUDGET_S` at boot; the deploy family's value is only the default.
    held_settle_budget_s: float
    # meiva:note
    # The environment an external harness session starts from (`child_env("runner", ...)`).
    harness_environ: Mapping[str, str]
    # meiva:note
    # The host process's own environment, which a spawned takeover relay's session environment is
    # projected from. The daemon root hands in its configuration's launch environment.
    environ: Mapping[str, str] = field(repr=False)
    # meiva:note
    # The generator the host draws identities and credentials from: a runtime's startup notice
    # key and each relay credential. The daemon root builds it once (a CSPRNG).
    rng: SecureRandom
    # meiva:note
    # The host process's pid, which a harness registration names as its controller.
    pid: int
    # meiva:note
    # The operating-system account's home (`~`), read once by the daemon root and handed to the
    # contexts the host builds.
    user_home: Path

    def resolve_slices(
        self,
        pins: Mapping[str, Any],
        plugin_pins: Mapping[str, Mapping[str, Any]],
        plugin_configs: Mapping[str, BaseModel] | None,
    ) -> AgentSlices:
        """Resolve the model at the existing slice boundary, with pins taking precedence."""
        brain = AgentBrain(pins.get("llm_model", self.default_model))
        return AgentSlices.resolve(
            pins,
            plugin_pins,
            plugin_configs=plugin_configs,
            brain=brain,
            default_reader=self.default_reader,
        )


@dataclass
class AgentRuntime:
    """The per-agent half of a turn, cached across turns.

    `fingerprint` is what makes the cache honest: it records the config this
    runtime was built FROM, so a turn whose freshly-read config disagrees
    rebuilds instead of running the old model.
    """

    fingerprint: str
    llm: BaseChatModel | None
    # meiva:note
    # The turn engine this runtime was built for, and the external session it drives.
    # Both belong to the cached runtime because an external session is expensive to
    # start and resumes by id: the cache's idle TTL and LRU bound is what closes it,
    # together with the explicit drops (terminate, restart, crash) and host shutdown.
    # A built-in runtime carries no bridge and never asks for one; harness is immutable
    # per agent, so a rebuilt runtime cannot switch engines under the same id.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    harness: Harness = Harness.MEIVA
    harness_bridge: TurnEngine | None = None
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class HostStats:
    """Counters the /healthz payload exposes — the cheap half of the cold-build
    observability, whose expensive half is the `host_agent_prepared` event.

    `wakes_skipped` is the one to read on a multi-runner cluster: the dispatcher
    pattern is cluster-wide, so every runner sees every agent's wake and each
    skips the ones it does not own.

    `config_rejected` counts wakes whose bound model configuration cannot build.
    `config_normalized` counts wakes whose stored `llm_model` pin named a
    withdrawn model and was bound as its registered fallback instead.
    """

    cache_hits: int = 0
    cache_misses: int = 0
    turns_started: int = 0
    wakes_skipped: int = 0
    config_rejected: int = 0
    config_normalized: int = 0
    handoff_settle_retries: int = 0
    handoff_settle_failures: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "turns_started": self.turns_started,
            "wakes_skipped": self.wakes_skipped,
            "config_rejected": self.config_rejected,
            "config_normalized": self.config_normalized,
            "handoff_settle_retries": self.handoff_settle_retries,
            "handoff_settle_failures": self.handoff_settle_failures,
        }


def admit_stored_model(
    pins: dict[str, Any],
    *,
    agent_id: int,
    stored: stored_config.StoredConfig,
    stats: HostStats,
    rejected: dict[int, str],
    normalized: dict[int, str],
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    default_model: str,
) -> bool:
    """Admit the stored model configuration a hosted wake is about to bind.

    Fail fast before ANY turn work — the status flip included: an overlay naming
    a model the registry does not know would otherwise explode inside
    `build_chat_model` on every wake (the dispatcher drops the task, the pending
    scan re-wakes the still-pending inbound, and the host loops on crash
    tracebacks — incident #2344). The effective model resolves exactly as the
    turn view does (overlay > birth > cluster default). A wake whose
    configuration cannot build is consumed without raising: the durable inbound
    stays pending, and a fixed overlay is served on the next scan.

    A pin naming a model the registry has since withdrawn is not refused: it is
    rewritten in place to its registered fallback, the same resolution
    `build_chat_model` would otherwise apply only at the final build. Binding
    the resolved id here means the turn view reads it, the usage attribution
    records it, and the exec children re-emitted from these pins inherit it —
    instead of a withdrawn pin leaking to every reader until the last build.

    Both outcomes are noted once per stored config state (fingerprint) in
    `rejected` / `normalized` and counted in `stats`; neither writes to the
    database. `pins` is mutated in place — the caller binds it after this
    returns True.

    Returns True when the wake may proceed, False when the configuration cannot
    build (the caller returns without a turn).
    """
    model = pins.get("llm_model") or default_model
    try:
        validate_model_launch(
            model=model,
            config=pins,
            catalog=catalog,
            keys=keys,
            llm_override=llm_override,
            harness=Harness(stored.harness),
        )
    except ValueError as exc:
        stats.config_rejected += 1
        if rejected.get(agent_id) != stored.fingerprint:
            rejected[agent_id] = stored.fingerprint
            logger.error(
                "hosted wake for agent {agent_id} rejected before turn — "
                "its model config cannot build: {reason}. Fix the agent's "
                "llm_model (restart(config_overlay=...) or the spawn "
                "overlay) and the next wake serves normally.",
                event="host_config_rejected",
                agent_id=agent_id,
                reason=str(exc),
            )
        return False
    rejected.pop(agent_id, None)
    if "llm_model" in pins:
        pinned_model = pins["llm_model"]
        resolved_model = resolve_available_model(pinned_model, models=catalog.models)
        if resolved_model != pinned_model:
            pins["llm_model"] = resolved_model
            stats.config_normalized += 1
            if normalized.get(agent_id) != stored.fingerprint:
                normalized[agent_id] = stored.fingerprint
                logger.warning(
                    "hosted wake for agent {agent_id} normalized its stored "
                    "llm_model {requested} -> {resolved} (withdrawn model); "
                    "the turn binds the registered fallback",
                    event="host_config_normalized",
                    agent_id=agent_id,
                    requested=pinned_model,
                    resolved=resolved_model,
                )
    return True


async def cached_runtime(
    runtimes: OrderedDict[int, AgentRuntime],
    stats: HostStats,
    agent_id: int,
    fingerprint: str,
    slices: AgentSlices,
    build: Callable[[int, str, AgentSlices], Awaitable[AgentRuntime]],
    evict: Callable[[], None],
) -> AgentRuntime:
    """Build and retain the existing cache entry; the host owns its lifetime."""
    cached = runtimes.get(agent_id)
    if cached is not None and cached.fingerprint == fingerprint:
        cached.last_used = time.monotonic()
        runtimes.move_to_end(agent_id)
        stats.cache_hits += 1
        return cached

    reason = "cold" if cached is None else "config_changed"
    stats.cache_misses += 1
    started = time.monotonic()
    runtime = await build(agent_id, fingerprint, slices)
    runtimes[agent_id] = runtime
    runtimes.move_to_end(agent_id)
    logger.info(
        "hosted runtime for agent {agent_id} built ({reason})",
        event="host_agent_prepared",
        agent_id=agent_id,
        reason=reason,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    evict()
    return runtime


# meiva:note
# Evict idle runtimes by age and least-recent use; misses rebuild on wake. Active
# runtimes survive both bounds, including turns longer than the idle TTL; completion
# refreshes idle time and LRU position before eviction, and an over-budget warm cache
# returns to its configured size as active runtimes settle. What it evicted is
# returned so the caller can close the sessions those runtimes hold: dropping the
# entry is what retires an external harness runtime.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def evict_runtimes(
    runtimes: OrderedDict[int, AgentRuntime],
    in_flight: set[int],
    *,
    policy: HostCachePolicy,
    now: float,
) -> list[tuple[int, AgentRuntime]]:
    evicted: list[tuple[int, AgentRuntime]] = []
    cutoff = now - policy.idle_ttl_seconds
    aged = [a for a, r in runtimes.items() if r.last_used < cutoff and a not in in_flight]
    for agent_id in aged:
        evicted.append((agent_id, runtimes.pop(agent_id)))
    cap = policy.size
    for agent_id in list(runtimes):
        if len(runtimes) <= cap:
            break
        if agent_id not in in_flight:
            evicted.append((agent_id, runtimes.pop(agent_id)))
    return evicted


async def read_last_active_at(pool: AsyncConnectionPool, agent_id: int) -> datetime | None:
    """This agent's real activity clock — `agents_meta.last_active_at`.

    Handed to `TurnScheduler` so an uncancellable-turn report can say how
    long the agent has actually been silent. Deliberately THIS column and not
    the `/api/agents` field of the same name: that one is
    `MAX(inbound_messages.created_at)` (`base/agents/observation/snapshot.py`) and goes
    stale during exactly the long turns where "is it wedged?" is a real
    question — issue #183. This column is written on every completed LLM step
    (`agent/graph/llm/node.py:persist_last_active`).

    Returns None when the row is gone; raising is left to the caller's
    best-effort wrapper, which runs on the shutdown path.
    """
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT last_active_at FROM agents_meta WHERE id = %s", (agent_id,))
        ).fetchone()
    return None if row is None else row[0]


# meiva:note
# How a retired session's exit status is recorded: (agent id, three-state code).
# A callable rather than the pool itself so the host can keep owning its connection
# policy and the retirement path stays testable without a database.
ExitRecorder = Callable[[int, "int | None"], Awaitable[None]]


# meiva:note
# Write how an agent's external harness session ended (task #5616): the code and the
# moment it was read, in one statement so the pair cannot disagree. `exit_code` is one
# of three states and the column comment is their only owner: None (still running /
# never recorded — the built-in meiva loop has no child), 0 (the CLI exited normally),
# non-zero (it exited abnormally). Only the host's session retirement calls this, and
# only for a session that closed. Raising is the caller's problem: it logs and
# continues, because a missing diagnostic must never cost the agent its teardown.
async def record_harness_exit(
    pool: AsyncConnectionPool, agent_id: int, exit_code: int | None
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agents_meta SET harness_exit_code = %s, harness_exit_at = now() WHERE id = %s",
            (exit_code, agent_id),
        )


async def build_runtime(
    agent_id: int,
    fingerprint: str,
    slices: AgentSlices,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    llm_override: str,
    permissions_notice_key: UUID,
    home: Path,
    harness: Harness = Harness.MEIVA,
    registry: SessionRegistry | None = None,
    registrations: RegistrationStore | None = None,
    registration_ttl_seconds: float,
    machine: str,
    harness_environ: Mapping[str, str],
    harness_root: BridgeRootValues,
    contributions: Iterable[HarnessContribution] = (),
    unsolicited: UnsolicitedSink | None = None,
) -> AgentRuntime:
    """Build the retained model after the host repaired its original admission."""
    llm, _binding = await boot_agent_scope(
        agent_id,
        slices.brain.llm_model,
        slices.overrides,
        catalog=catalog,
        keys=keys,
        llm_override=llm_override,
        permissions_notice_key=permissions_notice_key,
        home=home,
        harness=harness,
    )
    model = None
    effort = None
    if harness is not Harness.MEIVA:
        model, effort = catalog.models[slices.brain.llm_model].harness_model(
            slices.brain.llm_model,
            harness,
            slices.overrides.reasoning_effort,
        )
    return AgentRuntime(
        fingerprint=fingerprint,
        llm=llm,
        harness=harness,
        harness_bridge=harness_bridge_for(
            agent_id,
            harness,
            registry=registry,
            registrations=registrations,
            registration_ttl_seconds=registration_ttl_seconds,
            machine=machine,
            environ=harness_environ,
            root=harness_root,
            model=model,
            reasoning_effort=effort,
            skip_permissions=slices.harness_cli.harness_skip_permissions,
            contributions=contributions,
            unsolicited=unsolicited,
            home=home,
        ),
    )


def harness_bridge_for(
    agent_id: int,
    harness: Harness,
    *,
    registry: SessionRegistry | None,
    registrations: RegistrationStore | None,
    registration_ttl_seconds: float,
    machine: str,
    environ: Mapping[str, str],
    root: BridgeRootValues,
    model: str | None = None,
    reasoning_effort: str | None = None,
    skip_permissions: bool = True,
    contributions: Iterable[HarnessContribution] = (),
    unsolicited: UnsolicitedSink | None = None,
    home: Path,
) -> HarnessBridge | None:
    if harness is Harness.MEIVA:
        return None
    if registry is None:
        raise ValueError(
            f"an agent on harness {harness.value!r} needs the session registry its resume reads"
        )
    return bridge_for(
        harness,
        agent_id=agent_id,
        machine=machine,
        workspace=workspace_dir(agent_id, home=home),
        registry=registry,
        registrations=registrations,
        registration_ttl_seconds=registration_ttl_seconds,
        environ=environ,
        root=root,
        model=model,
        reasoning_effort=reasoning_effort,
        skip_permissions=skip_permissions,
        contributions=contributions,
        unsolicited=unsolicited,
    )


def refresh_cached_runtime(
    runtimes: OrderedDict[int, AgentRuntime],
    in_flight: set[int],
    agent_id: int,
    evict: Callable[[], None],
) -> None:
    """Refresh the retained model's recency after the owned turn has finished."""
    cached = runtimes.get(agent_id)
    if cached is not None:
        cached.last_used = time.monotonic()
        runtimes.move_to_end(agent_id)
    in_flight.discard(agent_id)
    evict()


# meiva:note
# The host's one owner of session shutdown. A runtime can be dropped from any sync
# path — idle eviction, terminate, restart, crash — while closing an external session
# is async and must not leak the CLI child process that holds the transcript, so the
# close runs as a task inside this component's own TaskGroup and is retained until the
# group joins at host shutdown. `start` runs at the top of every hosted turn, which is
# always before a runtime can be dropped; a retirement without it is a wiring defect
# and raises rather than silently leaving the session open.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
# meiva:note
# The host's owner of the harness I/O a turn must not wait for. Two kinds share it
# because they share the shape: work a session triggers that outlives the turn asking
# for it, retained in one task group and joined at shutdown.
#
# Closing a session is a child-process teardown, and the same agent can be woken again
# before it finishes: an evicted runtime is gone from the cache, so the next turn
# builds a fresh bridge and would start a second CLI on the same resumed session id
# while the first one is still being reaped. `retire` therefore keeps the close per
# agent, and `join` is what the next turn waits on before building.
#
# A report a session makes about work that ended after its turn — a background command
# — becomes an inbound. The session's read loop hands it here rather than writing from
# the loop, and a failed write is logged and dropped: the report is lost, and the
# session that made it must not die over a database error (#5663).
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
class HarnessSessions:
    def __init__(
        self, registrations: RegistrationStore, *, record_exit: ExitRecorder, clock: Clock
    ) -> None:
        self._registrations = registrations
        self._clock = clock
        # meiva:note
        # How a retired session's exit status reaches the agent row. Required rather
        # than optional: a host that forgets to wire it would silently stop recording
        # instead of failing at wiring time (task #5616).
        self._store_exit = record_exit
        self._tasks = asyncio.TaskGroup()
        self._started = False
        self._pending: set[asyncio.Task[BaseException | None]] = set()
        self._retiring: dict[int, asyncio.Task[BaseException | None]] = {}
        self.failures: list[BaseException] = []

    async def start(self) -> None:
        if self._started:
            return
        await self._tasks.__aenter__()
        self._started = True
        self._registrations.sweep(now=self._clock.now())

    def retire(
        self, agent_id: int, runtime: AgentRuntime, *, reason: str = "runtime retired"
    ) -> None:
        bridge = runtime.harness_bridge
        if bridge is None:
            return
        if not self._started:
            raise RuntimeError(
                "a harness session cannot be retired before the host's session owner started"
            )
        if agent_id in self._retiring:
            raise RuntimeError(f"agent {agent_id} already has a retiring harness session")
        task = self._tasks.create_task(
            self._close(agent_id, bridge, reason),
            name=f"harness-close-{runtime.harness.value}-{agent_id}",
        )
        self._pending.add(task)
        self._retiring[agent_id] = task
        task.add_done_callback(self._collect)

    # meiva:note
    # Wait for this agent's retired session to finish closing, if one is. The next turn
    # calls it before it builds a runtime, which is the only ordering that keeps one
    # resumed session id from having two CLIs at once. A cancelled waiter leaves the
    # close running, and a failed close keeps refusing admission on every later join.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    async def join(self, agent_id: int) -> None:
        task = self._retiring.get(agent_id)
        if task is None:
            return
        error = await asyncio.shield(task)
        if error is not None:
            raise error
        if self._retiring.get(agent_id) is task:
            del self._retiring[agent_id]

    def notify(
        self, agent_id: int, event: UnsolicitedOutput, *, db: Database, bus: EventBus
    ) -> None:
        if not self._started:
            raise RuntimeError(
                "a harness notice cannot be written before the host's session owner started"
            )
        task = self._tasks.create_task(
            self._notify(agent_id, event, db=db, bus=bus), name=f"harness-notice-{agent_id}"
        )
        self._pending.add(task)
        task.add_done_callback(self._collect)

    # meiva:note
    # The write runs in a worker thread: an inbound insert is a database round trip, and
    # the read loop that reported the event must not wait for one. A failure is logged
    # here and answered with None, so the shared collector treats it as a settled task
    # rather than as a session teardown that failed.
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    async def _notify(
        self, agent_id: int, event: UnsolicitedOutput, *, db: Database, bus: EventBus
    ) -> BaseException | None:
        try:
            await asyncio.to_thread(insert_harness_notice, db, bus, agent_id, event.text)
        except BaseException as error:
            logger.opt(exception=error).warning(
                "[{label}] {body}",
                label="harness",
                event="harness_notice_failed",
                body=(
                    f"agent {agent_id}: could not deliver unsolicited {event.source} "
                    f"output as an inbound; the report is lost"
                ),
            )
        return None

    def _collect(self, task: asyncio.Task[BaseException | None]) -> None:
        self._pending.discard(task)
        try:
            error = task.result()
        except asyncio.CancelledError as cancelled:
            error = cancelled
        if error is not None:
            self.failures.append(error)
            logger.opt(exception=error).warning(
                "[{label}] {body}",
                label="harness",
                event="harness_close_failed",
                task_name=task.get_name(),
                body=f"retiring a harness session raised: {error!r}",
            )

    # meiva:note
    # One agent's failed teardown is a retained admission fence, not a TaskGroup
    # failure that cancels the other agents' teardown tasks.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    async def _close(self, agent_id: int, bridge: TurnEngine, reason: str) -> BaseException | None:
        try:
            await bridge.close(reason=reason)
        except BaseException as error:
            return error
        # meiva:note
        # Only a close that succeeded reports an exit status: a failed teardown keeps
        # the previous value (or NULL) rather than recording a code nobody observed.
        await self._record_exit(agent_id, bridge.exit_code)
        return None

    # meiva:note
    # The exit status is a diagnostic, not part of the teardown contract: a write that
    # raises must not turn a successful close into a retained admission fence (this
    # task's return value is what the next `join` raises on), so it is logged and
    # dropped instead of propagated.
    async def _record_exit(self, agent_id: int, exit_code: int | None) -> None:
        try:
            await self._store_exit(agent_id, exit_code)
        except BaseException as error:
            logger.opt(exception=error).warning(
                "[{label}] {body}",
                label="harness",
                event="harness_exit_record_failed",
                body=f"recording exit code {exit_code!r} for agent {agent_id} raised: {error!r}",
            )

    async def aclose(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._tasks.__aexit__(None, None, None)
        if self.failures:
            logger.warning(
                "[{label}] {body}",
                label="harness",
                event="harness_close_failures",
                body=f"{len(self.failures)} session close(s) raised while running: "
                f"{self.failures!r}",
            )
