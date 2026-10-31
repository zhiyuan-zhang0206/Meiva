import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import {
  formatTurnSummary,
  groupIntoTurns,
  summarizeTurn,
  type TimelineGroup,
} from "./runs";

let idSeq = 1;
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

function kinds(groups: TimelineGroup[]): string[] {
  return groups.map((g) =>
    g.kind === "turn" ? `turn(${g.items.length}@${g.startIndex})` : `single(${g.item.kind}@${g.index})`,
  );
}

describe("groupIntoTurns", () => {
  it("collapseTurns off → every item is its own single group", () => {
    const items = [item("agent_reasoning"), item("agent_code"), item("agent_chat")];
    const groups = groupIntoTurns(items, { collapseTurns: false, liveIndex: null });
    expect(groups.every((g) => g.kind === "single")).toBe(true);
    expect(groups).toHaveLength(3);
  });

  it("folds a stretch of secondary items between two primary replies", () => {
    const items = [
      item("agent_chat"),
      item("agent_reasoning"),
      item("agent_code"),
      item("code_output"),
      item("agent_chat"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["single(agent_chat@0)", "turn(3@1)", "single(agent_chat@4)"]);
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(3);
    expect(turn.startIndex).toBe(1);
  });

  it("a lone secondary item becomes a turn block (always collapsible)", () => {
    const items = [item("agent_chat"), item("inbound_chat", "agent:9"), item("agent_chat")];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(1@1)",
      "single(agent_chat@2)",
    ]);
  });

  it("bare markers break turns and pass through as singles", () => {
    const items = [
      item("agent_reasoning"),
      item("agent_code"),
      item("system_marker", null),
      item("agent_reasoning"),
      item("code_output"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["turn(2@0)", "single(system_marker@2)", "turn(2@3)"]);
  });

  it("human inbound breaks a turn", () => {
    const items = [
      item("agent_reasoning"),
      item("agent_code"),
      item("inbound_chat", "user"),
      item("agent_reasoning"),
      item("agent_code"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["turn(2@0)", "single(inbound_chat@2)", "turn(2@3)"]);
  });

  it("streaming secondary items are always groupable — no peeling", () => {
    const items = [
      item("agent_reasoning"),
      item("agent_code"),
      item("code_output"),
      item("agent_reasoning"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: 3 });
    expect(kinds(groups)).toEqual(["turn(4@0)"]);
  });

  it("a single streaming secondary item becomes a turn block immediately", () => {
    const items = [
      item("agent_chat"),
      item("agent_reasoning"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: 1 });
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(1@1)",
    ]);
  });

  it("initial context items fold into a turn — system prompt and memories go into first detail block", () => {
    const items = [
      item("system_prompt"),
      item("inbound_compact_summary"),
      item("inbound_compact_request"),
      item("system_marker", "memory"),
      item("system_marker", "heartbeat"),
      item("system_marker", "agent_id"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual(["turn(6@0)"]);
    const turn = groups[0];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(6);
    expect(formatTurnSummary(turn.summary)).toBe("2 compact summaries · 1 memory · 2 system notes");
  });

  it("context-only items after real content fold normally (e.g. restart lifecycle marker + new system prompt)", () => {
    const items = [
      item("agent_chat"),
      item("system_marker", "lifecycle_restart"),
      item("system_prompt"),
      item("system_marker", "memory"),
      item("system_marker", "agent_memory"),
      item("agent_chat"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(4@1)",
      "single(agent_chat@5)",
    ]);
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.thinking).toBe(0);
    expect(turn.summary.code).toBe(0);
    expect(turn.summary.output).toBe(0);
    expect(formatTurnSummary(turn.summary)).toBe("2 memories · 1 system note");
  });

  it("context items mixed with agent actions before first primary — all fold together", () => {
    const items = [
      item("system_prompt"),
      item("system_marker", "memory"),
      item("agent_reasoning"),
      item("agent_code"),
      item("code_output"),
      item("agent_chat"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual([
      "turn(5@0)",
      "single(agent_chat@5)",
    ]);
    const turn = groups[0];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.total).toBe(5);
  });

  it("attach items fold into the turn's details block (user ruling 2026-08-30)", () => {
    const items = [
      item("agent_chat"),
      item("agent_code"),
      item("code_output"),
      item("attach"),
      item("agent_chat"),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual([
      "single(agent_chat@0)",
      "turn(3@1)",
      "single(agent_chat@4)",
    ]);
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(formatTurnSummary(turn.summary)).toBe("1 turn · 1 system note");
  });

  it("folds consecutive non-human wakes and non-user-facing agent_chat into one contiguous turn until user-facing reply", () => {
    const items = [
      item("inbound_chat", "user"),
      item("inbound_chat", "agent:10"),
      item("agent_reasoning"),
      item("agent_chat", null, undefined, { user_facing: false, payload: "internal ack" }),
      item("inbound_chat", "agent:11"),
      item("agent_chat", null, undefined, { user_facing: false, payload: "still internal" }),
      item("agent_chat", null, undefined, { user_facing: true, payload: "All workers done." }),
    ];
    const groups = groupIntoTurns(items, { collapseTurns: true, liveIndex: null });
    expect(kinds(groups)).toEqual([
      "single(inbound_chat@0)",
      "turn(5@1)",
      "single(agent_chat@6)",
    ]);
    const turn = groups[1];
    if (turn.kind !== "turn") throw new Error("expected turn");
    expect(turn.summary.agentMessages).toBe(2);
    expect(turn.summary.systemNotes).toBe(2);
    expect(formatTurnSummary(turn.summary)).toBe("1 turn · 2 system notes · 2 agent messages");
  });

  it("summarizeTurn keeps an earlier concurrent live execution clock when a later tool finishes", () => {
    const items = [
      { ...item("agent_code"), item_id: "6.0", code_elapsed_ms: 10 },
      { ...item("code_output"), item_id: "7.0", payload: "", execStartedAt: 123_456 },
      { ...item("agent_code"), item_id: "8.0", code_elapsed_ms: 5 },
      { ...item("code_output"), item_id: "9.0", payload: "usage", exec_ms: 12 },
    ];
    const summary = summarizeTurn(items);
    expect(summary.lastLiveKind).toBe("output");
    expect(summary.lastLiveStartedAt).toBe(123_456);
  });
});
