import { describe, expect, it } from "vitest";

import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { applySystemEvent, foldEvent, mergeSnapshotWithStreaming, type ThreadTimelineState } from "../../fold/timeline";

const beat = { role: "llm_keepalive", agent_id: 1, msg_idx: 5, content: "" } as const;
const waiting = () => applySystemEvent([], beat, 1000);

describe("model waiting keepalives", () => {
  it("creates a visible empty clock for a mid-request join, without appending or resetting it", () => {
    const first = waiting();
    expect(first).toHaveLength(1);
    expect(first[0]).toMatchObject({ item_id: "5.0", payload: "", llmWaiting: true, reasoningStartedAt: 1000 });
    expect(applySystemEvent(first, beat, 5000)).toBe(first);
    expect(first[0].reasoning_ms).toBeUndefined();
    expect(first[0].reasoning_tokens).toBeUndefined();
  });

  it.each<SystemEvent>([
    { role: "code_start", agent_id: 1, item_id: "5.0" },
    { role: "code_delta", agent_id: 1, item_id: "5.1", content: "print(3)" },
    { role: "chat_delta", agent_id: 1, item_id: "5.0", content: "hello" },
    { role: "reasoning_delta", agent_id: 1, item_id: "5.2", content: "summary" },
  ])("replaces waiting with real content ($role), and ignores later keepalives", (event) => {
    const items = applySystemEvent(waiting(), event, 2000);
    expect(items).toHaveLength(1);
    expect(items[0].llmWaiting).toBeUndefined();
    expect(applySystemEvent(items, beat, 3000)).toBe(items);
  });

  it.each<SystemEvent>([
    { role: "llm_done", agent_id: 1 },
    { role: "error", agent_id: 1, content: "failed" },
    { role: "cancelled", agent_id: 1 },
    { role: "exec_output_chunk", agent_id: 1, item_id: "6.0", content: "", keepalive: true },
  ])("removes waiting on $role", (event) => {
    const state: ThreadTimelineState = {
      items: waiting(), streamingIds: new Set(), streamingCode: false,
      turnActive: true, hasMoreOlder: false, olderFetchCount: 0, resetPending: false,
    };
    expect(foldEvent(state, event, 3000).items.some((item) => item.llmWaiting)).toBe(false);
  });

  it("removes waiting when the authoritative idle state settles a missed terminal event", () => {
    useTimelineStore.getState().switchThread(1, null, false);
    useTimelineStore.getState().processSseEvent(beat, 1000);
    expect(useTimelineStore.getState().items.some((item) => item.llmWaiting)).toBe(true);
    useTimelineStore.getState().settleTurn(2000);
    expect(useTimelineStore.getState().items).toEqual([]);
  });

  it("survives an in-flight snapshot and disappears once the message commits", () => {
    const items = waiting();
    expect(mergeSnapshotWithStreaming(items, [], 5)).toEqual(items);
    expect(mergeSnapshotWithStreaming(items, [], 6)).toEqual([]);
    const real: BackendTimelineItem = { ...items[0], llmWaiting: undefined, kind: "agent_code", payload: "print(3)", partial: false };
    expect(mergeSnapshotWithStreaming(items, [real], 6)).toEqual([real]);
  });
});
