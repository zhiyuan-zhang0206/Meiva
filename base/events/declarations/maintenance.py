from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


# meiva:note
# One ci_usage_daily event per reconciliation day (05:00-05:00 cluster time): the CI workflow's runs and ceil-billed minutes for the window, split by attribution and OS; the attribution source is the legacy [Meiva-<id>] prefix, read from PR titles and commit subjects only when present (new PR titles carry none — user ruling 2026-10-16). est_usd is the private-repo overage equivalent at the GitHub-hosted rates; the repo is currently public, so minutes are the billing fact. Per-agent detail lives in the attribution ledger, not here.
# meiva:link base/events/docs/maintenance-payloads.md
class CiUsageDaily(TypedDict):
    day: str
    window_start: str
    window_end: str
    runs: int
    attributed_runs: int
    unattributed_runs: int
    total_minutes: int
    attributed_minutes: int
    linux_minutes: int
    macos_minutes: int
    appended_runs: int
    est_usd: float


class DebtSweepDaily(TypedDict):
    day: str
    scan_status: str
    action: str
    worker_agent_id: int


# meiva:note
# PrFlowDaily carries a complete cluster-local day's snapshot. The exporter re-emits its trailing
# window for the Loki table. A day without merges has no latency percentiles.
# meiva:link base/events/docs/maintenance-payloads.md
class PrFlowDaily(TypedDict):
    day: str
    merged_count: int
    ready_to_merge_median_seconds: NotRequired[float]
    ready_to_merge_p90_seconds: NotRequired[float]


class UnderstandingEnqueueFailed(TypedDict):
    agent_id: int
    compact_version: int
    error: str


class UnderstandingChunkFailed(TypedDict):
    agent_id: int
    job_id: int
    attempts: int
    error: str


# meiva:note
# understanding_chunk_gap reports the stretches of a chunk that no node will describe — the runs after the first uncovered run of a chunk that overlapped existing nodes, and the turns a closing chunk's boundary snapshot lacks. The rest of the chunk was described.
# meiva:link base/events/docs/maintenance-payloads.md
class UnderstandingChunkGap(TypedDict):
    agent_id: int
    job_id: int
    gaps: str


class UnderstandingSnapshotLag(TypedDict):
    agent_id: int
    waited_seconds: float


class UnderstandingChunkSkipped(TypedDict):
    agent_id: int
    job_id: int
    reason: str


class UnderstandingGroupFailed(TypedDict):
    agent_id: int
    level: int
    open_nodes: int
    error: str


class UnderstandingCallRecordFailed(TypedDict):
    agent_id: int
    job_id: int
    error: str


# meiva:note
# understanding_rebuild_failed marks a rebuild closed as failed after its last allowed attempt; the agent's leaves stay as they were (no partial replacement), and the failure is never silent.
# meiva:link base/events/docs/maintenance-payloads.md
class UnderstandingRebuildFailed(TypedDict):
    agent_id: int
    rebuild_id: int
    attempts: int
    error: str


# meiva:note
# A level-1 node that landed before one that already has a parent reports understanding_leaf_out_of_order; nothing repairs it automatically and the agent's upper levels need a manual build.
# meiva:link base/events/docs/maintenance-payloads.md
class UnderstandingLeafOutOfOrder(TypedDict):
    agent_id: int
    job_id: int
    span_end: int


# meiva:note
# One agent-watch round: the duties judged, how many were silent, how many messages the ladder sent,
# and how many readings the host could not take. `blind` is a reading that was not available, never
# a duty that was healthy; a round that emitted nothing is indistinguishable from a round that
# judged nothing, which is why this name is declared rather than left to the round's own log line.
# meiva:link base/events/docs/maintenance-payloads.md
class AgentWatchRound(TypedDict):
    watched: int
    silent: int
    sent: int
    blind: int


# meiva:note
# One main-red-watch round: the fingerprints still open on `main`, how many cards this round
# opened, how many failures were classified as infrastructure, and how many post-merge workflows
# were past their staleness threshold. The heartbeat is the point: a schedule that stops taking its
# rounds looks exactly like a schedule with nothing to report, so a missing round has to be visible
# as a missing metric sample rather than as silence: `rounds` is always 1, the field
# `METRIC_DISPOSITION` maps to the counter `meiva_main_red_watch_round_rounds_total`, and the Grafana
# rule `meiva-ops-main-red-watch-silent` fires when that counter has no sample for an hour.
# meiva:link base/events/docs/maintenance-payloads.md
class TrunkRedWatchRound(TypedDict):
    rounds: int
    fingerprints: int
    confirmed: int
    infra: int
    stale: int
    # meiva:note
    # Emitted since the round learned to count them, and missing from this declaration until
    # 2026-11-08: the canonical types said four fields while the watch sent five. A declaration that
    # already disagrees with the payload is the wrong place to add a sixth.
    unnamed: int
    # meiva:note
    # The suspects the round carries, oldest first: fingerprint, first_seen_at, red_count and
    # opportunities (task #6488). `suspects_dropped` says how many the cap left out, because a
    # truncated list without a count reads as the whole set.
    suspects: NotRequired[list[dict[str, object]]]
    suspects_dropped: NotRequired[int]
    # meiva:note
    # `stale` counts every stream that gave no usable judgement, and that includes a workflow that
    # read fine but has not run -- silence is not blindness. The blindness card needs the narrower
    # number, so the round now states it: `unreadable` counts reads that raised, and `stream_errors`
    # names each one with its message (bounded to one entry per stream and 160 characters). Without
    # it, "stale: 4" is a count nobody can diagnose without the schedule's log (task #6523).
    unreadable: NotRequired[int]
    stream_errors: NotRequired[list[dict[str, object]]]


# meiva:note
# One external harness CLI convergence round: status ("ok" | "failed" | "skipped"),
# count of updated CLIs, count of failed CLIs, reason (when skipped), and list of CLI outcomes.
# meiva:link base/events/docs/maintenance-payloads.md
class HarnessCliUpdateRound(TypedDict):
    status: str
    updated_count: NotRequired[int]
    failed_count: NotRequired[int]
    reason: NotRequired[str]
    outcomes: NotRequired[list[dict[str, object]]]


# meiva:note
# One Claude Code login keepalive round: status ("ok" | "failed" | "skipped"), the reason it did not
# succeed (claude_not_found, not_logged_in, nonzero_exit, timeout, non_production), and the exit code
# of the keepalive command when it ran. The command's output never enters the payload.
# meiva:link base/events/docs/maintenance-payloads.md
class ClaudeAuthKeepaliveRound(TypedDict):
    status: str
    reason: NotRequired[str]
    exit_code: NotRequired[int]


class PluginCollectorCycle(TypedDict):
    collector: str
    outcome: Literal["ok", "failed", "rate_limited"]
    reason: str
    cycle: int
    # meiva:note
    # The schedule the cycle came from: `process` names the role (`schedule-runner`), so this is
    # where a reader tells four collectors apart. Absent on a manual run, which has no id.
    schedule_id: NotRequired[int]


EVENTS: dict[str, EventSpec] = {
    "harness_cli_update_round": telemetry_event(
        "harness_cli_update_round",
        "one external harness CLI update round — status, updated/failed counts, and outcome details",
        payload=HarnessCliUpdateRound,
        site="schedules/jobs/harness/harness_cli_update.py:execute_round (via emit_system_event)",
    ),
    "claude_auth_keepalive_round": telemetry_event(
        "claude_auth_keepalive_round",
        "one Claude Code login keepalive round — status, the reason it did not succeed, and the keepalive command's exit code",
        payload=ClaudeAuthKeepaliveRound,
        site="schedules/jobs/harness/claude_auth_keepalive.py:execute_round (via emit_system_event)",
    ),
    "agent_watch_round": telemetry_event(
        "agent_watch_round",
        "one agent-watch round — duties judged, silent, woken, and unreadable",
        payload=AgentWatchRound,
        site="schedules/jobs/watch/agent_watch.py:run_round (via emit_system_event)",
    ),
    "ci_usage_daily": telemetry_event(
        "ci_usage_daily",
        "daily CI-minute reconciliation totals (C9)",
        payload=CiUsageDaily,
        site="schedules/jobs/c9_report.py:report_day (positional emit)",
    ),
    "main_red_watch_round": telemetry_event(
        "main_red_watch_round",
        "one main-red-watch round — open fingerprints, cards opened, infra failures, stale schedules",
        payload=TrunkRedWatchRound,
        site="schedules/main-red-watch-schedule.py:watch_round (via emit_system_event)",
    ),
    "debt_sweep_daily": telemetry_event(
        "debt_sweep_daily",
        "daily tech-debt mechanical scan and clearing-worker dispatch",
        payload=DebtSweepDaily,
        site="schedules/jobs/debt_sweep.py:sweep_day (positional emit)",
    ),
    "pr_flow_daily": telemetry_event(
        "pr_flow_daily",
        "daily PR-flow aggregates — ready->merged percentiles "
        "(absolute gauges, one sample per complete day)",
        payload=PrFlowDaily,
        site="scripts/ci/pull_requests/pr_flow_export.py:emit_events (positional emit)",
    ),
    "label_generated": telemetry_event("label_generated", "label auto-generated", tier="noise"),
    "label_generate_failed": telemetry_event(
        "label_generate_failed", "label generation failed", tier="anomaly"
    ),
    "label_generate_skipped": telemetry_event(
        "label_generate_skipped", "label generation skipped", tier="noise"
    ),
    "label_generate_empty": telemetry_event(
        "label_generate_empty", "label generation empty", tier="noise"
    ),
    "label_generate_rejected": telemetry_event(
        "label_generate_rejected", "label generation rejected as not a label", tier="noise"
    ),
    "understanding_enqueue_failed": telemetry_event(
        "understanding_enqueue_failed",
        "a chunk-triggered understanding job could not be enqueued (best-effort; the next trigger covers the same stretch)",
        payload=UnderstandingEnqueueFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:enqueue_chunk",
    ),
    "understanding_chunk_failed": telemetry_event(
        "understanding_chunk_failed",
        "a queued understanding chunk failed (attempted once, never retried) and stays undescribed",
        payload=UnderstandingChunkFailed,
        tier="anomaly",
        site="services/agent_runner/understanding/chunk_consumer.py:settle_chunk_job",
    ),
    "understanding_chunk_skipped": telemetry_event(
        "understanding_chunk_skipped",
        "a queued understanding chunk was closed without a node (unsupported provider path or an empty chunk)",
        payload=UnderstandingChunkSkipped,
        tier="anomaly",
        site="services/agent_runner/understanding/chunk_consumer.py:settle_chunk_job",
    ),
    "understanding_chunk_gap": telemetry_event(
        "understanding_chunk_gap",
        "a chunk was described only in part: the listed stretches (overlap leftovers, turns missing from a closing snapshot) have no node",
        payload=UnderstandingChunkGap,
        tier="anomaly",
        site="services/agent_runner/understanding/chunk_consumer.py:_report_gaps",
    ),
    "understanding_snapshot_lag": telemetry_event(
        "understanding_snapshot_lag",
        "a compaction stamped its boundary before the checkpoint held the last message (the bounded wait timed out)",
        payload=UnderstandingSnapshotLag,
        tier="anomaly",
        site="agent/hooks/understanding_chunks.py:await_snapshot",
    ),
    "understanding_group_failed": telemetry_event(
        "understanding_group_failed",
        "an upper-level understanding grouping check failed; the level's open nodes stay ungrouped until its next check threshold of new nodes arrives",
        payload=UnderstandingGroupFailed,
        tier="anomaly",
        site="services/agent_runner/understanding/group_consumer.py:_check_level",
    ),
    "understanding_call_record_failed": telemetry_event(
        "understanding_call_record_failed",
        "the audit rows of an understanding call could not be written; the node or group is kept and only the call record is lost",
        payload=UnderstandingCallRecordFailed,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:write_chunk_calls",
    ),
    "understanding_rebuild_failed": telemetry_event(
        "understanding_rebuild_failed",
        "an understanding rebuild failed on its last allowed attempt and was closed as failed",
        payload=UnderstandingRebuildFailed,
        tier="anomaly",
        site="services/agent_runner/understanding/chunk_consumer.py:Consumer._process_rebuild",
    ),
    "understanding_leaf_out_of_order": telemetry_event(
        "understanding_leaf_out_of_order",
        "an understanding leaf landed before an already grouped one; the agent's upper levels need a manual build",
        payload=UnderstandingLeafOutOfOrder,
        tier="anomaly",
        site="base/agents/history/hierarchy/chunks.py:write_group_nodes",
    ),
    "plugin_collector_cycle": telemetry_event(
        "plugin_collector_cycle",
        "one interval collector finished a refresh cycle, in both outcomes — the only record that says the loop ran at all; three intervals without one is a collector that is alive but producing nothing",
        payload=PluginCollectorCycle,
        tier="observation",
        site="meiva_builtins/plugins/meiva_usage/runner.py:_emit_cycle",
    ),
}
