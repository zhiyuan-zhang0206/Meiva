// runs.ts — pure grouping / classification unit tests. No DOM.

import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import {
  classifyItem,
  formatTurnSummary,
  inboundKind,
  summarizeTurn,
} from "./runs";

// Minimal item factory — only the fields classification reads (kind + source),
// plus whatever the caller overrides (duration / sdk_calls fields for the
// timing/SDK-aggregation tests below).
function item(
  kind: BackendTimelineItem["kind"],
  source: string | null = null,
  id = `${idSeq++}.0`,
  extra: Partial<BackendTimelineItem> = {},
): BackendTimelineItem {
  return {
    item_id: id,
    kind,
    source,
    payload: "",
    created_at: "2026-07-21T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...extra,
  };
}
let idSeq = 1;

describe("inboundKind", () => {
  it("maps agent / human / system sources", () => {
    expect(inboundKind("agent:42")).toBe("agent");
    expect(inboundKind("external_agent:claude_code")).toBe("external_agent");
    expect(inboundKind("external_agent:codex:run-42")).toBe("external_agent");
    expect(inboundKind("user")).toBe("human");
    expect(inboundKind("system")).toBe("system");
    expect(inboundKind("system:restart")).toBe("system");
    expect(inboundKind("watcher:7")).toBe("system");
    expect(inboundKind("shell:3")).toBe("system");
    expect(inboundKind("schedule:9")).toBe("system");
    expect(inboundKind("self:update")).toBe("system");
    expect(inboundKind(null)).toBe("system");
  });
});

describe("classifyItem", () => {
  it("folds a pre-token model wait into the secondary turn without counting a completed reasoning round", () => {
    const waiting = item("agent_reasoning", null, "5.0", {
      llmWaiting: true,
      reasoningStartedAt: 1000,
    });
    expect(classifyItem(waiting)).toBe("secondary");
    const summary = summarizeTurn([waiting]);
    expect(summary.thinking).toBe(0);
    expect(summary.settledActiveMs).toBeNull();
    expect(summary.priorActiveMs).toBe(0);
    expect(summary.liveSegmentStartMs).toBe(1000);
  });

  it("agent_chat and human inbound are primary; non-user-facing agent_chat is secondary", () => {
    expect(classifyItem(item("agent_chat"))).toBe("primary");
    expect(classifyItem(item("agent_chat", null, undefined, { user_facing: true }))).toBe("primary");
    expect(classifyItem(item("agent_chat", null, undefined, { user_facing: false }))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "user"))).toBe("primary");
  });

  it("agent + system inbound are secondary", () => {
    expect(classifyItem(item("inbound_chat", "agent:5"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "external_agent:claude_code"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "system"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "system:upload"))).toBe("secondary");
    expect(classifyItem(item("inbound_chat", "watcher:1"))).toBe("secondary");
  });

  it("thinking / code / output / compact / system_prompt / attach are secondary", () => {
    for (const k of [
      "agent_reasoning",
      "agent_code",
      "code_output",
      "inbound_compact_summary",
      "inbound_compact_request",
      "system_prompt",
      "attach",
    ] as const) {
      expect(classifyItem(item(k))).toBe("secondary");
    }
  });

  it("system markers stay secondary regardless of their renderer family", () => {
    for (const source of ["heartbeat", "memory", "lifecycle_terminate", "background_task", "scheduled_wake", null, "future_source"]) {
      expect(classifyItem(item("system_marker", source))).toBe("secondary");
    }
  });
});

describe("summarizeTurn / formatTurnSummary", () => {
  it("counts action items (thinking/code/output) — the specific metrics shown in the header", () => {
    const run = [
      item("agent_reasoning"),
      item("agent_code"),
      item("code_output"),
      item("agent_reasoning"),
      item("inbound_chat", "agent:2"),
      item("system_marker", "heartbeat"),
    ];
    const s = summarizeTurn(run);
    expect(s.total).toBe(6);
    expect(s.thinking).toBe(2);
    expect(s.code).toBe(1);
    expect(s.output).toBe(1);
    // The heartbeat note lands in the systemNotes bucket — every member kind
    // is counted somewhere so the header is never blank.
    expect(formatTurnSummary(s)).toBe("2 turns · 1 system note · 1 agent message");
  });

  it("collapses action items into one work-rounds count", () => {
    const s = summarizeTurn([
      item("agent_code"),
      item("agent_code"),
      item("code_output"),
    ]);
    expect(s.thinking).toBe(0);
    expect(s.code).toBe(2);
    expect(s.output).toBe(1);
    // Two rounds of code-writing (no reasoning emitted) — one "2 turns" label.
    expect(s.turns).toBe(2);
    expect(formatTurnSummary(s)).toBe("2 turns");
  });

  it("a turn with no actions still summarizes every member (never an empty header)", () => {
    const s = summarizeTurn([
      item("inbound_chat", "agent:1"),
      item("system_marker", "heartbeat"),
    ]);
    expect(s.thinking).toBe(0);
    expect(s.code).toBe(0);
    expect(s.output).toBe(0);
    expect(formatTurnSummary(s)).toBe("1 system note · 1 agent message");
  });

  it("never labels the system prompt (task #3557) — the compact summary label stays", () => {
    const s = summarizeTurn([
      item("system_prompt"),
      item("inbound_compact_summary"),
    ]);
    const line = formatTurnSummary(s);
    // The always-present prompt carries no information in the collapsed line…
    expect(line).not.toContain("system prompt");
    // …while the compact-summary marker stays (user decision: humans read it
    // as "this agent has compacted").
    expect(line).toBe("compact summary");
  });

  it("a turn with only thinking shows the rounds count", () => {
    const s = summarizeTurn([
      item("agent_reasoning"),
      item("agent_reasoning"),
      item("agent_reasoning"),
    ]);
    expect(formatTurnSummary(s)).toBe("3 turns");
  });
});

describe("summarizeTurn — active-segment timing", () => {
  it("computes duration from the segment's first item start (subtracting committed duration) to its last item end without splitting multi-step waits", () => {
    const t0 = Date.parse("2026-11-21T10:00:00.000Z");
    const run = [
      item("agent_reasoning", null, "1.0", {
        created_at: new Date(t0 + 8_000).toISOString(),
        reasoning_ms: 8_000,
      }),
      item("agent_code", null, "1.1", {
        created_at: new Date(t0 + 11_000).toISOString(),
        code_elapsed_ms: 3_000,
      }),
      item("code_output", null, "2.0", {
        created_at: new Date(t0 + 45_000).toISOString(),
        exec_ms: 34_000,
      }),
      item("agent_reasoning", null, "3.0", {
        created_at: new Date(t0 + 55_000).toISOString(),
        reasoning_ms: 5_000,
      }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(55_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(t0);
  });

  it("measures a single committed reasoning/code/output item via created_at - duration to created_at", () => {
    const end = Date.parse("2026-11-21T10:00:10.000Z");
    const reasoning = summarizeTurn([
      item("agent_reasoning", null, "a", { created_at: new Date(end).toISOString(), reasoning_ms: 6_000 }),
    ]);
    expect(reasoning.settledActiveMs).toBe(6_000);
    expect(reasoning.priorActiveMs).toBe(0);
    expect(reasoning.liveSegmentStartMs).toBe(end - 6_000);

    const code = summarizeTurn([
      item("agent_code", null, "b", { created_at: new Date(end).toISOString(), code_elapsed_ms: 5_000 }),
    ]);
    expect(code.settledActiveMs).toBe(5_000);
    expect(code.liveSegmentStartMs).toBe(end - 5_000);

    const output = summarizeTurn([
      item("code_output", null, "c", { created_at: new Date(end).toISOString(), exec_ms: 4_000 }),
    ]);
    expect(output.settledActiveMs).toBe(4_000);
    expect(output.liveSegmentStartMs).toBe(end - 4_000);
  });

  it("handles frontend-frozen reasoningElapsedMs / codeElapsedMs (created_at at start) and execElapsedMs (created_at at end)", () => {
    const start = Date.parse("2026-11-21T10:00:00.000Z");
    const frozenReasoning = summarizeTurn([
      item("agent_reasoning", null, "a", {
        created_at: new Date(start).toISOString(),
        reasoningElapsedMs: 3_000,
      }),
    ]);
    expect(frozenReasoning.settledActiveMs).toBe(3_000);
    expect(frozenReasoning.liveSegmentStartMs).toBe(start);

    const frozenCode = summarizeTurn([
      item("agent_code", null, "b", {
        created_at: new Date(start).toISOString(),
        codeElapsedMs: 2_000,
      }),
    ]);
    expect(frozenCode.settledActiveMs).toBe(2_000);
    expect(frozenCode.liveSegmentStartMs).toBe(start);

    const frozenExec = summarizeTurn([
      item("code_output", null, "c", {
        created_at: new Date(start + 7_000).toISOString(),
        execElapsedMs: 7_000,
      }),
    ]);
    expect(frozenExec.settledActiveMs).toBe(7_000);
    expect(frozenExec.liveSegmentStartMs).toBe(start);
  });

  it("sums 10m work + 1h idle + peer inbound_chat + 10m work to 20m (excluding inter-turn idle)", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const run = [
      item("agent_code", "Bash", "h10.0", { created_at: new Date(t0).toISOString() }),
      item("code_output", null, "h20.0", { created_at: new Date(t0 + 10 * MIN).toISOString(), exec_ms: 10 * MIN }),
      item("inbound_chat", "agent:228", "h30.0", { created_at: new Date(t0 + 70 * MIN).toISOString() }),
      item("agent_code", "Bash", "h40.0", { created_at: new Date(t0 + 71 * MIN).toISOString() }),
      item("code_output", null, "h50.0", { created_at: new Date(t0 + 80 * MIN).toISOString(), exec_ms: 9 * MIN }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(20 * MIN);
    expect(s.priorActiveMs).toBe(10 * MIN);
    expect(s.liveSegmentStartMs).toBe(t0 + 70 * MIN);
  });

  it("splits across hidden agent_chat close -> 1h idle -> hidden heartbeat resume without a visible inbound item", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const closedOnly = [
      item("agent_reasoning", null, "h10.0", { created_at: new Date(t0 + 5 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
      item("agent_code", "Bash", "h20.0", { created_at: new Date(t0 + 6 * MIN).toISOString() }),
      item("code_output", null, "h30.0", { created_at: new Date(t0 + 9 * MIN).toISOString(), exec_ms: 3 * MIN }),
      item("agent_chat", null, "h40.0", { user_facing: false, created_at: new Date(t0 + 10 * MIN).toISOString() }),
    ];
    const sClosed = summarizeTurn(closedOnly);
    expect(sClosed.settledActiveMs).toBe(10 * MIN);
    expect(sClosed.priorActiveMs).toBe(10 * MIN);
    expect(sClosed.liveSegmentStartMs).toBeNull();

    const resumed = [
      ...closedOnly,
      item("agent_reasoning", null, "h50.0", { created_at: new Date(t0 + 75 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
      item("code_output", null, "h60.0", { created_at: new Date(t0 + 80 * MIN).toISOString(), exec_ms: 5 * MIN }),
    ];
    const sResumed = summarizeTurn(resumed);
    expect(sResumed.settledActiveMs).toBe(20 * MIN);
    expect(sResumed.priorActiveMs).toBe(10 * MIN);
    expect(sResumed.liveSegmentStartMs).toBe(t0 + 70 * MIN);
  });

  it("does not split same-step hidden agent_chat + agent_code from its long-running code_output", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const run = [
      item("agent_chat", null, "h100.0", { user_facing: false, created_at: new Date(t0).toISOString() }),
      item("agent_code", "Bash", "h100.1", { created_at: new Date(t0 + 1_000).toISOString() }),
      item("code_output", null, "h120.0", {
        created_at: new Date(t0 + 8 * MIN + 1_000).toISOString(),
        exec_ms: 8 * MIN,
      }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(8 * MIN + 1_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(t0);
  });

  it("keeps same-step hidden agent_chat + agent_code joined across compact-prefixed historical item_ids (s1.prev.5.0 + s1.prev.5.1)", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const run = [
      item("agent_chat", null, "s1.prev.5.0", { user_facing: false, created_at: new Date(t0).toISOString() }),
      item("agent_code", "Bash", "s1.prev.5.1", { created_at: new Date(t0 + 2_000).toISOString() }),
      item("code_output", null, "s1.prev.6.0", {
        created_at: new Date(t0 + 5 * MIN + 2_000).toISOString(),
        exec_ms: 5 * MIN,
      }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(5 * MIN + 2_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(t0);
  });

  it("isolates timestamped non-wake system_markers after a closed segment so a later hidden wake never absorbs idle time", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const closedPlusMarkers = [
      item("agent_reasoning", null, "h10.0", { created_at: new Date(t0 + 5 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
      item("agent_code", "Bash", "h20.0", { created_at: new Date(t0 + 6 * MIN).toISOString() }),
      item("code_output", null, "h30.0", { created_at: new Date(t0 + 9 * MIN).toISOString(), exec_ms: 3 * MIN }),
      item("agent_chat", null, "h40.0", { user_facing: false, created_at: new Date(t0 + 10 * MIN).toISOString() }),
      item("system_marker", "task", "h41.0", { created_at: new Date(t0 + 10 * MIN + 1_000).toISOString(), show_timestamp: true }),
      item("system_marker", "compact_failed", "h42.0", { created_at: new Date(t0 + 10 * MIN + 2_000).toISOString(), show_timestamp: true }),
      item("system_marker", null, "_marker.err", { payload: "error:boom", created_at: new Date(t0 + 10 * MIN + 3_000).toISOString(), show_timestamp: true }),
    ];
    const sIdle = summarizeTurn(closedPlusMarkers);
    expect(sIdle.settledActiveMs).toBe(10 * MIN);
    expect(sIdle.priorActiveMs).toBe(10 * MIN);
    expect(sIdle.liveSegmentStartMs).toBeNull();

    const resumed = [
      ...closedPlusMarkers,
      item("agent_reasoning", null, "h50.0", { created_at: new Date(t0 + 75 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
    ];
    const sResumed = summarizeTurn(resumed);
    expect(sResumed.settledActiveMs).toBe(15 * MIN);
    expect(sResumed.priorActiveMs).toBe(10 * MIN);
    expect(sResumed.liveSegmentStartMs).toBe(t0 + 70 * MIN);
  });

  it("hard-closes segments on lifecycle_terminate / lifecycle_interrupt and splits on lifecycle_restart / lifecycle_resurrect / lifecycle_fork", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const terminated = [
      item("agent_code", "Bash", "1.0", { created_at: new Date(t0).toISOString() }),
      item("code_output", null, "2.0", { created_at: new Date(t0 + 10 * MIN).toISOString(), exec_ms: 10 * MIN }),
      item("system_marker", "lifecycle_terminate", "3.0", { created_at: new Date(t0 + 10 * MIN).toISOString(), show_timestamp: true }),
    ];
    const sTerm = summarizeTurn(terminated);
    expect(sTerm.settledActiveMs).toBe(10 * MIN);
    expect(sTerm.priorActiveMs).toBe(10 * MIN);
    expect(sTerm.liveSegmentStartMs).toBeNull();

    const resurrected = [
      ...terminated,
      item("system_marker", "lifecycle_resurrect", "4.0", { created_at: new Date(t0 + 130 * MIN).toISOString(), show_timestamp: true }),
      item("agent_code", "Bash", "5.0", { created_at: new Date(t0 + 131 * MIN).toISOString() }),
      item("code_output", null, "6.0", { created_at: new Date(t0 + 135 * MIN).toISOString(), exec_ms: 4 * MIN }),
    ];
    expect(summarizeTurn(resurrected).settledActiveMs).toBe(15 * MIN);

    const directRestart = [
      item("agent_code", "Bash", "1.0", { created_at: new Date(t0).toISOString() }),
      item("code_output", null, "2.0", { created_at: new Date(t0 + 10 * MIN).toISOString(), exec_ms: 10 * MIN }),
      item("system_marker", "lifecycle_restart", "3.0", { created_at: new Date(t0 + 130 * MIN).toISOString(), show_timestamp: true }),
      item("agent_reasoning", null, "4.0", { created_at: new Date(t0 + 135 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
      item("system_marker", "lifecycle_fork", "5.0", { created_at: new Date(t0 + 195 * MIN).toISOString(), show_timestamp: true }),
      item("agent_reasoning", null, "6.0", { created_at: new Date(t0 + 198 * MIN).toISOString(), reasoning_ms: 3 * MIN }),
    ];
    expect(summarizeTurn(directRestart).settledActiveMs).toBe(18 * MIN);

    const interruptedThenHiddenWake = [
      item("agent_code", "Bash", "1.0", { created_at: new Date(t0).toISOString() }),
      item("system_marker", "lifecycle_interrupt", "2.0", { created_at: new Date(t0 + 4 * MIN).toISOString(), show_timestamp: true }),
      item("agent_reasoning", null, "3.0", { created_at: new Date(t0 + 67 * MIN).toISOString(), reasoning_ms: 3 * MIN }),
    ];
    expect(summarizeTurn(interruptedThenHiddenWake).settledActiveMs).toBe(7 * MIN);
  });

  it("splits and deduplicates queued heartbeat system_markers without counting inter-heartbeat idle time", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const run = [
      item("system_marker", "heartbeat", "1.0", { created_at: new Date(t0).toISOString(), show_timestamp: true }),
      item("system_marker", "heartbeat", "2.0", { created_at: new Date(t0 + 30 * MIN).toISOString(), show_timestamp: true }),
      item("agent_reasoning", null, "3.0", { created_at: new Date(t0 + 30 * MIN + 10_000).toISOString(), reasoning_ms: 10_000 }),
      item("system_marker", "heartbeat", "4.0", { created_at: new Date(t0 + 90 * MIN).toISOString(), show_timestamp: true }),
      item("agent_reasoning", null, "5.0", { created_at: new Date(t0 + 90 * MIN + 15_000).toISOString(), reasoning_ms: 15_000 }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(25_000);
    expect(s.priorActiveMs).toBe(10_000);
    expect(s.liveSegmentStartMs).toBe(t0 + 90 * MIN);
  });

  it("clamps measured inverted timestamps to 0ms, returns null for unmeasured 0ms single-point segments, and ignores non-positive *StartedAt anchors", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const measuredInverted = summarizeTurn([
      item("agent_reasoning", null, "1.0", {
        created_at: new Date(t0).toISOString(),
        reasoningStartedAt: t0 + 10_000,
        reasoning_ms: 0,
      }),
    ]);
    expect(measuredInverted.settledActiveMs).toBe(0);
    expect(measuredInverted.liveSegmentStartMs).toBe(t0 + 10_000);

    const unmeasuredInverted = summarizeTurn([
      item("agent_reasoning", null, "1.0", {
        created_at: new Date(t0).toISOString(),
        reasoningStartedAt: t0 + 10_000,
      }),
    ]);
    expect(unmeasuredInverted.settledActiveMs).toBeNull();
    expect(unmeasuredInverted.liveSegmentStartMs).toBe(t0 + 10_000);

    const sameSecondUnmeasured = summarizeTurn([
      item("agent_reasoning", null, "1.0", { created_at: "2026-11-22T04:08:02.000Z" }),
      item("agent_chat", null, "1.1", { user_facing: false, created_at: "2026-11-22T04:08:02.000Z" }),
    ]);
    expect(sameSecondUnmeasured.settledActiveMs).toBeNull();

    const sameSecondMeasuredZero = summarizeTurn([
      item("agent_reasoning", null, "1.0", { created_at: "2026-11-22T04:08:02.000Z", reasoning_ms: 0 }),
    ]);
    expect(sameSecondMeasuredZero.settledActiveMs).toBe(0);

    const subSecondPositiveSpan = summarizeTurn([
      item("agent_reasoning", null, "1.0", { created_at: "2026-11-22T04:08:02.000Z" }),
      item("agent_chat", null, "1.1", { user_facing: false, created_at: "2026-11-22T04:08:02.400Z" }),
    ]);
    expect(subSecondPositiveSpan.settledActiveMs).toBe(400);

    const zeroAnchor = summarizeTurn([
      item("agent_reasoning", null, "1.0", {
        created_at: new Date(t0 + 4_000).toISOString(),
        reasoning_ms: 4_000,
        reasoningStartedAt: 0,
      }),
    ]);
    expect(zeroAnchor.settledActiveMs).toBe(4_000);
    expect(zeroAnchor.liveSegmentStartMs).toBe(t0);
  });

  it("excludes queue idle time between consecutive inbound_chat wakes with no agent output and ignores missing timestamps on empty inbound segments", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const run = [
      item("inbound_chat", "agent:10", "1.0", { created_at: null }),
      item("inbound_chat", "agent:11", "2.0", { created_at: new Date(t0).toISOString() }),
      item("inbound_chat", "agent:12", "3.0", { created_at: new Date(t0 + 5 * MIN).toISOString() }),
      item("inbound_chat", "agent:13", "4.0", { created_at: new Date(t0 + 65 * MIN).toISOString() }),
      item("agent_code", "Bash", "5.0", { created_at: new Date(t0 + 65 * MIN + 5_000).toISOString() }),
      item("code_output", null, "6.0", { created_at: new Date(t0 + 65 * MIN + 20_000).toISOString(), exec_ms: 15_000 }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(20_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(t0 + 65 * MIN);
  });

  it("keeps inbound_compact_request and inbound_compact_summary in the same segment", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const run = [
      item("inbound_compact_request", null, "1.0", { created_at: new Date(t0).toISOString() }),
      item("inbound_compact_summary", null, "2.0", { created_at: new Date(t0 + 4_000).toISOString() }),
      item("agent_reasoning", null, "3.0", { created_at: new Date(t0 + 10_000).toISOString(), reasoning_ms: 6_000 }),
    ];
    const s = summarizeTurn(run);
    expect(s.settledActiveMs).toBe(10_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(t0);
  });

  it("excludes standing context items (system_prompt and show_timestamp=false system_marker) from active segments", () => {
    const oldSpawn = Date.parse("2026-11-18T00:00:00.000Z");
    const workStart = Date.parse("2026-11-21T10:00:00.000Z");
    const s = summarizeTurn([
      item("system_prompt", null, "0.0", { created_at: null }),
      item("system_marker", "agent_id", "1.0", {
        created_at: new Date(oldSpawn).toISOString(),
        show_timestamp: false,
      }),
      item("agent_reasoning", null, "10.0", {
        created_at: new Date(workStart + 4_000).toISOString(),
        reasoning_ms: 4_000,
      }),
    ]);
    expect(s.settledActiveMs).toBe(4_000);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(workStart);
  });

  it("returns null settledActiveMs when any active segment lacks start or end timestamps, and null priorActiveMs when a prior active segment lacks timestamps", () => {
    const t = "2026-11-21T10:00:00.000Z";
    const missingBoth = summarizeTurn([
      item("agent_code", null, "a", { created_at: null }),
      item("code_output", null, "b", { created_at: null }),
    ]);
    expect(missingBoth.settledActiveMs).toBeNull();
    expect(missingBoth.priorActiveMs).toBe(0);
    expect(missingBoth.liveSegmentStartMs).toBeNull();

    const missingStart = summarizeTurn([
      item("agent_code", null, "a", { created_at: null }),
      item("code_output", null, "b", { created_at: t }),
    ]);
    expect(missingStart.settledActiveMs).toBeNull();
    expect(missingStart.liveSegmentStartMs).toBeNull();

    const missingEnd = summarizeTurn([
      item("agent_code", null, "a", { created_at: t }),
      item("code_output", null, "b", { created_at: null }),
    ]);
    expect(missingEnd.settledActiveMs).toBeNull();
    expect(missingEnd.liveSegmentStartMs).toBe(Date.parse(t));

    const missingPrior = summarizeTurn([
      item("agent_code", null, "1.0", { created_at: null }),
      item("inbound_chat", "agent:2", "2.0", { created_at: t }),
      item("agent_code", null, "3.0", { created_at: "2026-11-21T10:00:05.000Z" }),
    ]);
    expect(missingPrior.settledActiveMs).toBeNull();
    expect(missingPrior.priorActiveMs).toBeNull();
    expect(missingPrior.liveSegmentStartMs).toBe(Date.parse(t));
  });

  it("takes the latest end across out-of-order parallel tool completions", () => {
    const t0 = Date.parse("2026-11-21T10:00:00.000Z");
    const s = summarizeTurn([
      item("agent_code", "Bash", "1.0", { created_at: new Date(t0).toISOString() }),
      item("code_output", null, "2.0", { created_at: new Date(t0 + 15_000).toISOString(), exec_ms: 15_000 }),
      item("code_output", null, "3.0", { created_at: new Date(t0 + 5_000).toISOString(), exec_ms: 5_000 }),
    ]);
    expect(s.settledActiveMs).toBe(15_000);
    expect(s.liveSegmentStartMs).toBe(t0);
  });

  it("a system-note-only turn summarizes as '1 system note' (never blank)", () => {
    const one = summarizeTurn([item("system_marker", "sdk_hint")]);
    expect(one.systemNotes).toBe(1);
    expect(one.settledActiveMs).toBeNull();
    expect(formatTurnSummary(one)).toBe("1 system note");
    const several = summarizeTurn([
      item("system_marker", "lifecycle_restart"),
      item("inbound_chat", "system"),
    ]);
    expect(several.systemNotes).toBe(2);
    expect(formatTurnSummary(several)).toBe("2 system notes");
  });
});

describe("summarizeTurn — SDK call aggregation", () => {
  it("aggregates call counts by method across code items, sourced from the backend AST field (not a text scan)", () => {
    const run = [
      item("agent_code", null, undefined, {
        payload: "meiva.files.read('a')",
        sdk_calls: [{ method: "files.read", count: 1 }],
      }),
      item("agent_code", null, undefined, {
        payload: "meiva.files.read('b')\nmeiva.shell.run('ls')",
        sdk_calls: [
          { method: "files.read", count: 1 },
          { method: "shell.run", count: 3 },
        ],
      }),
    ];
    const s = summarizeTurn(run);
    expect(s.sdkCalls).toEqual([
      { method: "files.read", count: 2 },
      { method: "shell.run", count: 3 },
    ]);
  });

  it("groups SDK calls by namespace after aggregating multiple code items", () => {
    const run = [
      item("agent_code", null, undefined, {
        sdk_calls: [
          { method: "shell.run", count: 4 },
          { method: "files.write", count: 2 },
          { method: "agents.spawn", count: 1 },
        ],
      }),
      item("agent_code", null, undefined, {
        sdk_calls: [
          { method: "shell.run", count: 1 },
          { method: "files.read", count: 3 },
          { method: "files.write", count: 2 },
          { method: "agents.spawn", count: 1 },
        ],
      }),
    ];
    expect(summarizeTurn(run).sdkCalls).toEqual([
      { method: "agents.spawn", count: 2 },
      { method: "files.write", count: 4 },
      { method: "files.read", count: 3 },
      { method: "shell.run", count: 5 },
    ]);
  });

  it("trusts a genuinely empty backend sdk_calls ([]) as zero — does not regex-scan the payload text", () => {
    const run = [
      item("agent_code", null, undefined, {
        payload: "# example: meiva.files.read('x')\nprint('hi')",
        sdk_calls: [],
      }),
    ];
    expect(summarizeTurn(run).sdkCalls).toEqual([]);
  });

  it("skips (does not regex-scan) an item whose sdk_calls field is absent, e.g. never committed", () => {
    const run = [item("agent_code", null, undefined, { payload: "meiva.agents.spawn()", sdk_calls: null })];
    expect(summarizeTurn(run).sdkCalls).toEqual([]);
  });

  it("a turn with no code items has an empty sdkCalls list", () => {
    expect(summarizeTurn([item("agent_reasoning")]).sdkCalls).toEqual([]);
  });
});

describe("summarizeTurn — external harness items", () => {
  it("uses created_at as start for an unanswered external harness tool call", () => {
    const ts = "2026-10-25T01:02:03.000Z";
    const s = summarizeTurn([
      item("agent_code", "Bash", "2.0", {
        created_at: ts,
        activity_pending: true,
      }),
    ]);
    expect(s.code).toBe(1);
    expect(s.liveSegmentStartMs).toBe(Date.parse(ts));
    expect(s.priorActiveMs).toBe(0);
  });

  it("computes active duration across settled external harness items", () => {
    const s = summarizeTurn([
      item("agent_reasoning", null, "1.0", {
        created_at: "2026-10-25T01:02:03.000Z",
        reasoning_ms: 2_500,
      }),
      item("agent_code", "Bash", "2.0", {
        created_at: "2026-10-25T01:02:05.500Z",
        activity_pending: null,
      }),
      item("code_output", "Bash", "3.0", {
        created_at: "2026-10-25T01:02:06.000Z",
        exec_ms: 500,
      }),
    ]);
    expect(s.settledActiveMs).toBe(5_500);
    expect(s.priorActiveMs).toBe(0);
    expect(s.liveSegmentStartMs).toBe(Date.parse("2026-10-25T01:02:00.500Z"));
  });
});
