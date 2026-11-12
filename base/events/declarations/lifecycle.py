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
# `agent/graph/claim/harness_interrupt.py:interrupt_engine`, the rest in
# `base/lm/harness/process.py:settle_close` and the session closers that call it.
# meiva:link base/lm/harness/docs/harness.md
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
        "interrupting an external harness turn raised; the durable interrupt row stays pending "
        "and claim dispatches it again on the next pass",
        tier="anomaly",
    ),
    "harness_interrupt_note_failed": telemetry_event(
        "harness_interrupt_note_failed",
        "a confirmed stop's passive note could not be written to the timeline; the row stays "
        "pending, unflagged, and is delivered with its entry along with the next input",
        tier="anomaly",
    ),
    "harness_delivery_pool_timeout": telemetry_event(
        "harness_delivery_pool_timeout",
        "delivering the inbox to an external harness could not borrow a database connection; "
        "nothing was reserved and the same attempt is made again after a short wait",
        tier="anomaly",
    ),
    "harness_unsettled_outcome_failed": telemetry_event(
        "harness_unsettled_outcome_failed",
        "an invocation failed and the outcome of the inputs it had handed to the harness could not "
        "be recorded; they stay claimed and the next harness turn reconciles them against the "
        "harness's record",
        tier="anomaly",
    ),
    "harness_input_reconciled": telemetry_event(
        "harness_input_reconciled",
        "an input handed to an external harness and never acknowledged was judged against the "
        "harness's own durable record: in_context settles it, not_found returns it to the queue to "
        "be delivered again, persisted_undelivered leaves it claimed for the harness to deliver",
        tier="noise",
    ),
    "harness_reconcile_failed": telemetry_event(
        "harness_reconcile_failed",
        "the harness has no durable record to ask about inputs handed to it (no session record or "
        "file), or its session could not be retired first, or a record that exists could not be read; "
        "nothing is guessed: the inputs are consumed with the uncertainty marker, or, when an "
        "existing record could not be read, left claimed for the next wake",
        tier="anomaly",
    ),
    "harness_event_stale": telemetry_event(
        "harness_event_stale",
        "a frame of an earlier harness process arrived after a later generation began and was "
        "dropped before it could settle an input or end a turn",
        tier="noise",
    ),
    "harness_subagent_call_not_found": telemetry_event(
        "harness_subagent_call_not_found",
        "a CLI sub-agent's activity named a spawning call that this agent's timeline does not hold "
        "(never written, or reset since); the line was written without a parent, once per call id",
        tier="noise",
    ),
    "harness_input_requeued": telemetry_event(
        "harness_input_requeued",
        "an external harness refused an input or declared it lost; the inbound went back to the "
        "queue at its original position and is delivered again",
        tier="noise",
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
    "harness_exit_record_failed": telemetry_event(
        "harness_exit_record_failed",
        "a harness session closed cleanly but its child exit status could not be written; the "
        "agent row keeps what it had and NULL still means never observed",
        tier="anomaly",
    ),
    "harness_usage_log_failed": telemetry_event(
        "harness_usage_log_failed",
        "an external harness turn finished but its usage could not be priced; the reply stands "
        "and the accounting row for that turn is missing",
        tier="anomaly",
    ),
    "harness_registration_renewal_failed": telemetry_event(
        "harness_registration_renewal_failed",
        "an external harness session could not renew its registration",
        tier="anomaly",
    ),
    "harness_registration_unreadable": telemetry_event(
        "harness_registration_unreadable",
        "a harness registration row could not be read during the sweep and is left in place",
        tier="anomaly",
    ),
    "handoff_instructions_file_failed": telemetry_event(
        "handoff_instructions_file_failed",
        "the handoff instructions could not be saved to the agent's workspace; the first message "
        "carries no pointer to the file, so the agent cannot re-read its instructions after a "
        "compaction",
        tier="anomaly",
    ),
    "harness_notice_failed": telemetry_event(
        "harness_notice_failed",
        "an unsolicited provider report could not be written as an inbound; the report is lost "
        "and the agent that was waiting on it is never told",
        tier="anomaly",
    ),
    "harness_background_work_lost": telemetry_event(
        "harness_background_work_lost",
        "an external harness CLI process was replaced while it held background work (a command, a "
        "sub-agent, a timer); the agent's next input carries a note naming what was lost",
    ),
    "harness_background_work_evicted": telemetry_event(
        "harness_background_work_evicted",
        "the host evicted a hosted runtime whose external CLI still held background work, because "
        "the cache was over its size bound with nothing idle left to evict; its next input "
        "carries a note naming what was lost",
        tier="anomaly",
    ),
    "harness_image_not_delivered": telemetry_event(
        "harness_image_not_delivered",
        "an image of a message could not be given to an external harness (an Antigravity turn that "
        "ended or never ran before the image could be steered in, or a refused steer); the text of "
        "the message was delivered and names the image, whose path is in the message",
        tier="anomaly",
    ),
    "agy_subagent_descriptor_unreadable": telemetry_event(
        "agy_subagent_descriptor_unreadable",
        "an Antigravity sub-agent descriptor file was not the recorded shape, so that sub-agent's "
        "text and tool calls are not shown on the timeline; the turn is unaffected",
    ),
}
