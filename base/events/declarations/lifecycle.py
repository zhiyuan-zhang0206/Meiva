"""Agent process lifecycle events: spawn, resurrect, terminate, launch, wake, node and halt."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class Halt(TypedDict):
    """`halt` payload — compact/idle detection reads the body."""

    body: str


class ServiceStarted(TypedDict):
    """`service_started` payload — base/log/__init__.py."""

    name: str
    pid: int


class AgentSpawned(TypedDict):
    """`agent_spawned` payload — base/agents/birth/spawn.py."""

    spawner: str  # "user" | "agent:<id>" | "scheduler" | ...
    forked_from: int | None


class NodeExitEntry(TypedDict):
    """One node's exit inside an aggregated per-turn `node_exit` event."""

    node: str
    outcome: str  # ok | cancelled
    duration_seconds: float


class NodeExit(TypedDict):
    """`node_exit` payload — one aggregated event per graph turn (agent/graph/node_log.py)."""

    count: int
    nodes: list[NodeExitEntry]


# meiva:note
# These payloads carry what the bridge learned, never what it hoped. `settled=None`
# means the adapter never observed the process tree (its provider SDK exposes the pid
# through a private structure), so it is reported as unknown rather than as reaped; a
# close that leaves processes behind names how many. Emit sites: the interrupt pair in
# `agent/graph/claim/harness_turn.py:interrupt_engine`, the rest in
# `base/lm/harness/process.py:settle_close` and the session closers that call it.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class HarnessInterrupt(TypedDict):
    agent_id: int
    settled: bool | None
    turn_id: str | None


class HarnessCloseUnobserved(TypedDict):
    harness: str
    agent_id: int | None


class HarnessCloseSettled(TypedDict):
    harness: str
    agent_id: int | None
    waited_ms: float


class HarnessCloseResidue(TypedDict):
    harness: str
    agent_id: int | None
    remaining: int
    waited_ms: float


EVENTS: dict[str, EventSpec] = {
    # node / process lifecycle
    "node_enter": telemetry_event(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the event stream (PR #1758); log files only",
        destination="file",
        tier="noise",
    ),
    "node_exit": telemetry_event(
        "node_exit", "LangGraph node exited", payload=NodeExit, tier="noise"
    ),
    "service_started": telemetry_event(
        "service_started",
        "gateway/daemon started",
        payload=ServiceStarted,
        tier="noise",
        persist=True,
    ),
    "halt": telemetry_event(
        "halt",
        "turn stopped (idle/compact/system)",
        payload=Halt,
        tier="noise",
        persist=True,
    ),
    "agent_spawned": telemetry_event(
        "agent_spawned",
        "agent process started",
        payload=AgentSpawned,
        persist=True,
    ),
    "agent_resurrected": telemetry_event("agent_resurrected", "agent resurrected", persist=True),
    "auto_resurrect_failed": telemetry_event(
        "auto_resurrect_failed",
        "an automatic resurrect failed for a possibly transient reason; the "
        "triggering inbound stays queued for a later or manual resurrect",
    ),
    # agent lifecycle / state
    "wake_degraded": telemetry_event(
        "wake_degraded",
        "RedisInboundListener wake path degraded (instant pub/sub wake off)",
        tier="anomaly",
    ),
    "wake_restored": telemetry_event(
        "wake_restored",
        "RedisInboundListener wake path recovered (clean consume restored instant wake)",
        tier="noise",
    ),
    "launch_failures": telemetry_event(
        "launch_failures",
        "this start could not read the launch-failure report, so the unlaunched sessions it "
        "recorded stay unreported to the rollout aftermath block",
        tier="anomaly",
    ),
    "harness_interrupt": telemetry_event(
        "harness_interrupt",
        "an external harness turn was interrupted; the adapter's settle answer is carried as "
        "it came back",
        payload=HarnessInterrupt,
        tier="noise",
    ),
    "harness_interrupt_failed": telemetry_event(
        "harness_interrupt_failed",
        "interrupting an external harness turn raised; the durable cancel row stays pending "
        "and claim dispatches it again on the next pass",
        tier="anomaly",
    ),
    "harness_close_settled": telemetry_event(
        "harness_close_settled",
        "an external harness session closed and none of its observed processes survived",
        payload=HarnessCloseSettled,
        tier="noise",
    ),
    "harness_close_unobserved": telemetry_event(
        "harness_close_unobserved",
        "an external harness session closed without ever observing its process tree; the "
        "close answers unknown, not settled",
        payload=HarnessCloseUnobserved,
        tier="noise",
    ),
    "harness_close_residue": telemetry_event(
        "harness_close_residue",
        "closing an external harness session left processes running after the settle window",
        payload=HarnessCloseResidue,
        tier="anomaly",
    ),
    "harness_close_failed": telemetry_event(
        "harness_close_failed",
        "closing an external harness session raised",
        tier="anomaly",
    ),
    "harness_close_failures": telemetry_event(
        "harness_close_failures",
        "external harness session closes raised while the agent host was running",
        tier="anomaly",
    ),
    "harness_usage_log_failed": telemetry_event(
        "harness_usage_log_failed",
        "an external harness turn finished but its usage could not be priced; the reply stands "
        "and the accounting row for that turn is missing",
        tier="anomaly",
    ),
    "harness_registration_unreadable": telemetry_event(
        "harness_registration_unreadable",
        "a harness registration row could not be read during the sweep and is left in place",
        tier="anomaly",
    ),
}
