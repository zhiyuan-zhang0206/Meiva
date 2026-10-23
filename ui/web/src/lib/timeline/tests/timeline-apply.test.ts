import { describe, expect, it } from "vitest";

import { applySystemEvent } from "../timeline";
import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";

describe("applySystemEvent (clocks, compaction, and terminal states)", () => {

  it("code_start starts a live code clock that survives its own event and deltas", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    expect(items[0].codeStartedAt).toBeTypeOf("number");
    expect(items[0].codeElapsedMs).toBeUndefined();
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "x = 1",
    });

    expect(items[0].codeStartedAt).toBeTypeOf("number");
    expect(items[0].codeElapsedMs).toBeUndefined();

    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    expect(items[0].codeStartedAt).toBeUndefined();
    expect(items[0].codeElapsedMs).toBeTypeOf("number");
  });

  it("a second code_start freezes the previous block's clock but keeps its own live", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, { role: "code_start", item_id: "5.2", agent_id: 1 });
    const first = items.find((it) => it.item_id === "5.1");
    const second = items.find((it) => it.item_id === "5.2");
    expect(first?.codeStartedAt).toBeUndefined();
    expect(first?.codeElapsedMs).toBeTypeOf("number");
    expect(second?.codeStartedAt).toBeTypeOf("number");
    expect(second?.codeElapsedMs).toBeUndefined();
  });

  it("consecutive exec_starts with same item_id upsert — at most 1 per id", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "code_output", item_id: "6.0" });

    items = applySystemEvent(items, { role: "exec_start", item_id: "7.0", agent_id: 1 });
    expect(items).toHaveLength(2);
  });

  it("exec_start after exec_output creates a fresh placeholder", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "old exec output",
    });
    items = applySystemEvent(items, { role: "exec_start", item_id: "7.0", agent_id: 1 });

    expect(items.filter((it) => it.kind === "code_output" && it.item_id === "7.0")).toHaveLength(1);
    expect(items.filter((it) => it.kind === "code_output")).toHaveLength(2);
  });

  it("exec_output does not erase other system_markers (error / cancelled etc.)", () => {
    const errorMarker: BackendTimelineItem = {
      item_id: "_marker.err",
      kind: "system_marker",
      source: null,
      payload: "error:test error",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    const cancelledMarker: BackendTimelineItem = {
      item_id: "_marker.cancelled",
      kind: "system_marker",
      source: null,
      payload: "cancelled",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    let items: BackendTimelineItem[] = [errorMarker, cancelledMarker];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "out",
    });
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ kind: "system_marker", payload: "error:test error" });
    expect(items[1]).toMatchObject({ kind: "system_marker", payload: "cancelled" });
  });

  it("does not mutate the input list", () => {
    const original: BackendTimelineItem[] = [];
    const ev: SystemEvent = {
      role: "exec_output",
      item_id: "5.0",
      agent_id: 1,
      content: "x",
    };
    applySystemEvent(original, ev);
    expect(original).toHaveLength(0);
  });

  it("chat_start + chat_delta concatenate into agent_chat (by item_id)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "hi ",
    });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "user",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_chat", payload: "hi user" });
  });

  describe("partial flag", () => {
    it("chat_delta arrives without chat_start → partial=true (user joined mid-stream and missed start)", () => {
      const ev: SystemEvent = {
        role: "chat_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "...",
      };
      const [first] = applySystemEvent([], ev);
      expect(first).toMatchObject({ kind: "agent_chat", payload: "...", partial: true });
    });

    it("item created by chat_start carries no partial flag (full stream)", () => {
      const items = applySystemEvent([], {
        role: "chat_start",
        item_id: "5.0",
        agent_id: 1,
      });
      expect(items[0].partial).toBeUndefined();
    });

    it("partial=true accumulates more delta and stays partial (sticky)", () => {
      let items = applySystemEvent([], {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "x",
      });
      items = applySystemEvent(items, {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "y",
      });
      expect(items[0]).toMatchObject({ payload: "xy", partial: true });
    });

    it("partial behavior is symmetric across reasoning / chat / code kinds", () => {
      const r = applySystemEvent([], {
        role: "reasoning_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "a",
      });
      expect(r[0].partial).toBe(true);
      const c = applySystemEvent([], {
        role: "code_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "a",
      });
      expect(c[0].partial).toBe(true);
      const t = applySystemEvent([], {
        role: "chat_delta",
        item_id: "5.2",
        agent_id: 1,
        content: "a",
      });
      expect(t[0].partial).toBe(true);
    });
  });
});

  describe("SSE arrival order — tail append", () => {

    it("in-order arrivals append in arrival order", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "reasoning_delta",
        item_id: "5.0",
        agent_id: 1,
        content: "thinking",
      });
      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "reply",
      });
      items = applySystemEvent(items, {
        role: "code_delta",
        item_id: "5.2",
        agent_id: 1,
        content: "print(1)",
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1", "5.2"]);
      expect(items.map((i) => i.kind)).toEqual([
        "agent_reasoning",
        "agent_chat",
        "agent_code",
      ]);
    });

    it("existing item (same id) updates in place, position unchanged", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "reasoning_start",
        item_id: "5.0",
        agent_id: 1,
      });
      items = applySystemEvent(items, {
        role: "chat_start",
        item_id: "5.1",
        agent_id: 1,
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1"]);

      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "more text",
      });
      expect(items.map((i) => i.item_id)).toEqual(["5.0", "5.1"]);
      expect(items[1].payload).toBe("more text");
    });

    it("exec_start appends a code_output placeholder after the streamed message", () => {
      let items: BackendTimelineItem[] = [];
      items = applySystemEvent(items, {
        role: "chat_delta",
        item_id: "5.1",
        agent_id: 1,
        content: "reply",
      });
      items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });

      expect(items.map((i) => i.item_id)).toEqual(["5.1", "6.0"]);
      expect(items[1]).toMatchObject({ kind: "code_output", payload: "" });
      expect(items[1].execStartedAt).toBeGreaterThan(0);
    });
  });
