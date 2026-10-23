import { describe, expect, it } from "vitest";

import {
  isEventForThread,
  mergeSnapshotWithStreaming,
} from "../timeline";
import type { BackendTimelineItem } from "../../contracts/types";

let idCounter = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    item_id: `fix.${++idCounter}`,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

describe("mergeSnapshotWithStreaming", () => {

  it("empty prev + snapshot → result equals snapshot", () => {
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
    ];
    expect(mergeSnapshotWithStreaming([], snapshot, 0)).toEqual(snapshot);
  });

  it("prev items not in the snapshot are kept (scroll-loaded history survives an incremental snapshot)", () => {

    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old msg", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_chat", payload: "old reply" }),
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
      item({ item_id: "6.0", kind: "agent_chat", payload: "newest" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 7);
    expect(result.map((i) => i.item_id)).toEqual(["1.0", "2.0", "5.0", "6.0"]);
  });

  it("a committed item missing from the snapshot is kept (no stale-drop — snapshots are incremental)", () => {

    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old", inbound_id: 1 }),
      item({ item_id: "5.1", kind: "agent_chat", payload: "committed earlier" }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "inbound_chat", payload: "recent", inbound_id: 5 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 7);

    expect(result.map((i) => i.item_id)).toEqual(["1.0", "5.0", "5.1"]);
  });

  it("prev and snapshot share an item_id → snapshot version wins, no duplicate from prev", () => {

    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "thought 1" }),
      item({ item_id: "2.1", kind: "agent_chat", payload: "reply 1" }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "hi", inbound_id: 1 }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "thought 1" }),
      item({ item_id: "2.1", kind: "agent_chat", payload: "reply 1" }),
      item({ item_id: "3.0", kind: "inbound_chat", payload: "second", inbound_id: 2 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toEqual(snapshot);
    expect(result.filter((i) => i.payload === "thought 1")).toHaveLength(1);
    expect(result.filter((i) => i.payload === "reply 1")).toHaveLength(1);
  });

  it("narrow stale rule: a partial not at the future position and not tracked is dropped", () => {

    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old", inbound_id: 1 }),
      item({
        item_id: "5.0",
        kind: "agent_reasoning",
        payload: "thinking 1/2 ...",
        partial: true,
      }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "old envelope", inbound_id: 1 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toHaveLength(1);
    expect(result[0].payload).toBe("old envelope");
    expect(result.find((i) => i.item_id === "5.0")).toBeUndefined();
  });

  it("llm-done reload: after snapshot replaces the streaming id, prev partial vanishes automatically", () => {

    const prev = [
      item({
        item_id: "5.0",
        kind: "agent_reasoning",
        payload: "thinking partial...",
        partial: true,
      }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "agent_reasoning", payload: "thinking complete" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toEqual(snapshot);
    expect(result.find((i) => i.partial)).toBeUndefined();
  });

  it("same content → returns the same prev reference (issue 2 flicker optimization)", () => {

    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const b = item({ item_id: "5.1", kind: "agent_code", payload: "x = 1" });
    const prev = [a, b];
    const snapshot = [a, b];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toBe(prev);
  });

  it("content changed → returns a new array (must flip the reference when commit replaces partial)", () => {
    const prev = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "partial" }),
    ];
    const snapshot = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "final" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result[0].payload).toBe("final");
  });

  it("all ephemeral markers not in the snapshot are preserved (error / cancelled carry 'this happened' info)", () => {

    const prev = [
      item({
        item_id: "_marker.execstart",
        kind: "system_marker",
        payload: "exec_start",
      }),
      item({
        item_id: "_marker.err",
        kind: "system_marker",
        payload: "error:something blew up",
      }),
      item({
        item_id: "_marker.cancel",
        kind: "system_marker",
        payload: "cancelled",
      }),
    ];
    const snapshot: BackendTimelineItem[] = [];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toHaveLength(3);
  });
  it("ephemeral marker kept; stale partial dropped (narrow rule, not hard reset)", () => {
    const prev = [
      item({ item_id: "_marker.execstart", kind: "system_marker", payload: "exec_start" }),
      item({ item_id: "9.0", kind: "agent_chat", payload: "streaming…", partial: true }),
    ];
    const snapshot: BackendTimelineItem[] = [];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);

    expect(result.map((i) => i.item_id)).toEqual(["_marker.execstart"]);
  });

  it("does not mutate input", () => {
    const prev = [item({ item_id: "5.0", kind: "agent_chat" })];
    const snapshot = [item({ item_id: "1.0", kind: "inbound_chat", inbound_id: 1 })];
    const prevCopy = [...prev];
    const snapshotCopy = [...snapshot];
    mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(prev).toEqual(prevCopy);
    expect(snapshot).toEqual(snapshotCopy);
  });

  it("narrow stale rule: multiple stale partials not in snapshot are discarded, snapshot left clean", () => {

    const prev = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:user", inbound_id: 1 }),
      item({ item_id: "5.0", kind: "agent_code", payload: "print('z')", partial: true }),
      item({ item_id: "3.0", kind: "agent_chat", payload: "reply text", partial: true }),
      item({ item_id: "4.0", kind: "agent_reasoning", payload: "thinking", partial: true }),
    ];
    const snapshot = [
      item({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:user", inbound_id: 1 }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result.map((i) => i.item_id)).toEqual(["1.0"]);
  });

  it("merge: _marker.* not in snapshot → kept as ephemeral (error etc.)", () => {

    const prev: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "old" }),
      {
        item_id: "_marker.something",
        kind: "system_marker",
        source: null,
        payload: "error:test failure",
        created_at: "2026-01-01T00:00:00Z",
        inbound_id: null,
        show_timestamp: true,
      },
    ];
    const snapshot: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "old" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);

    expect(result).toHaveLength(2);
    expect(result[1].payload).toBe("error:test failure");
  });

  it("merge KEEPS pre-compact items — compaction is reconciled by the store's compact_done hard reset, not the merge", () => {

    const prev: BackendTimelineItem[] = [
      item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact", inbound_id: 1 }),
      item({ item_id: "91.0", kind: "agent_reasoning", payload: "pre-compact thinking" }),
      item({ item_id: "_marker.err", kind: "system_marker", payload: "error:pre-compact error" }),
    ];
    const snapshot: BackendTimelineItem[] = [
      item({
        item_id: "1.0",
        kind: "system_marker",
        payload: "[summary]",
      }),
      item({ item_id: "2.0", kind: "agent_chat", payload: "tail" }),
    ];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);

    expect(result.map((i) => i.item_id)).toEqual([
      "1.0", "2.0",
      "90.0", "91.0",
      "_marker.err",
    ]);
  });

});

describe("isEventForThread", () => {

  it("same agent_id → true", () => {
    expect(isEventForThread({ agent_id: 42 }, 42)).toBe(true);
  });

  it("different agent_id → false", () => {
    expect(isEventForThread({ agent_id: 42 }, 99)).toBe(false);
  });

  it("activeThreadId=null → false (uninitialized)", () => {
    expect(isEventForThread({ agent_id: 42 }, null)).toBe(false);
  });

  it("agent_id=0 system signal → passes through (independent of activeThreadId)", () => {

    expect(isEventForThread({ agent_id: 0 }, 42)).toBe(true);
    expect(isEventForThread({ agent_id: 0 }, null)).toBe(true);
  });
});
