import { describe, expect, it } from "vitest";

import {
  applySystemEvent,
  mergeSnapshotWithStreaming,
  parseItemId,
  sortByItemId,
} from "../timeline";
import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";

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

describe("parseItemId — boundary reinforcement", () => {

  it("one part non-finite (e.g. '5.x') → null (pins || cannot become &&)", () => {
    expect(parseItemId("5.x")).toBeNull();
    expect(parseItemId("x.5")).toBeNull();
  });
});

describe("sortByItemId — comparator reinforcement", () => {

  it("two ephemeral markers both parseItemId=null → keep original order (comparator returns 0)", () => {
    const m1 = item({ item_id: "_marker.first", kind: "system_marker", payload: "a" });
    const m2 = item({ item_id: "_marker.second", kind: "system_marker", payload: "b" });
    const sorted = sortByItemId([m1, m2]);
    expect(sorted[0]).toBe(m1);
    expect(sorted[1]).toBe(m2);

    const sorted2 = sortByItemId([m2, m1]);
    expect(sorted2[0]).toBe(m2);
    expect(sorted2[1]).toBe(m1);
  });

  it("one stable + one ephemeral mixed → ephemeral must be after stable (pins the pb===null branch)", () => {
    const m = item({ item_id: "_marker.x", kind: "system_marker", payload: "marker" });
    const s = item({ item_id: "5.0", kind: "agent_chat", payload: "stable" });

    const sorted = sortByItemId([m, s]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "_marker.x"]);

    const sorted2 = sortByItemId([s, m]);
    expect(sorted2.map((i) => i.item_id)).toEqual(["5.0", "_marker.x"]);
  });

  it("same msg_idx, different block_idx → sorted by block_idx (pins that msg_idx-equal branch falls to block compare)", () => {
    const a = item({ item_id: "5.2", kind: "agent_code", payload: "block 2" });
    const b = item({ item_id: "5.0", kind: "agent_chat", payload: "block 0" });
    const c = item({ item_id: "5.1", kind: "agent_reasoning", payload: "block 1" });
    const sorted = sortByItemId([a, b, c]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "5.1", "5.2"]);
  });

  it("input already ascending by block_idx → no swap (pins - cannot become +)", () => {
    const x = item({ item_id: "5.0", payload: "first" });
    const y = item({ item_id: "5.2", payload: "second" });
    const sorted = sortByItemId([x, y]);
    expect(sorted.map((i) => i.item_id)).toEqual(["5.0", "5.2"]);
  });
});

describe("createStartItem dedupe-guard reinforcement", () => {

  it("two chat_starts under same item_id → items length still 1, payload unchanged (pins dup guard)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });

    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "halfway written",
    });
    expect(items).toHaveLength(1);
    expect(items[0].payload).toBe("halfway written");

    items = applySystemEvent(items, { role: "chat_start", item_id: "5.0", agent_id: 1 });
    expect(items).toHaveLength(1);
    expect(items[0].payload).toBe("halfway written");
  });

  it("same item_id × twice for code_start + reasoning_start equally no-op (covers all three kinds)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    expect(items).toHaveLength(1);

    items = applySystemEvent(items, { role: "reasoning_start", item_id: "5.0", agent_id: 1 });
    items = applySystemEvent(items, { role: "reasoning_start", item_id: "5.0", agent_id: 1 });
    expect(items).toHaveLength(2);
  });
});

describe("upsertItemById reinforcement", () => {

  it("exec_output same id replaces items[0] (idx=0 boundary) → length still 1, payload is new value", () => {

    let items: BackendTimelineItem[] = [
      item({ item_id: "6.0", kind: "code_output", payload: "old output" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "new envelope",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ item_id: "6.0", payload: "new envelope" });
  });

  it("exec_output same id replaces an item in the middle (idx>0) → position unchanged, only payload updated", () => {
    let items: BackendTimelineItem[] = [
      item({ item_id: "5.0", kind: "agent_chat", payload: "chat" }),
      item({ item_id: "6.0", kind: "code_output", payload: "old output" }),
      item({ item_id: "7.0", kind: "agent_chat", payload: "more chat" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "fresh envelope",
    });
    expect(items).toHaveLength(3);
    expect(items[1]).toMatchObject({ item_id: "6.0", payload: "fresh envelope" });
  });
});

describe("insertSorted unparseable-id reinforcement", () => {

  it("chat_delta with unparseable id (not _marker, split fails) → silent append, no throw (pins !newParsed early return)", () => {

    const existing = item({ item_id: "5.0", kind: "agent_chat", payload: "first" });
    const result = applySystemEvent([existing], {
      role: "chat_delta",
      item_id: "not-an-id",
      agent_id: 1,
      content: "hi",
    });
    expect(result).toHaveLength(2);

    expect(result[1].item_id).toBe("not-an-id");
    expect(result[1].payload).toBe("hi");
  });
});

describe("ephemeral marker merge reinforcement", () => {

  it("every ephemeral marker survives the merge (error/cancelled/exec_start all visible)", () => {

    const err = item({
      item_id: "_marker.err",
      kind: "system_marker",
      payload: "error:boom",
    });
    const cancelled = item({
      item_id: "_marker.cancel",
      kind: "system_marker",
      payload: "cancelled",
    });
    const exec_start = item({
      item_id: "_marker.exec",
      kind: "system_marker",
      payload: "exec_start",
    });
    const result = mergeSnapshotWithStreaming(
      [err, cancelled, exec_start],
      [],
      0,
    );
    expect(result).toHaveLength(3);
    const payloads = result.map((it) => it.payload);
    expect(payloads).toContain("error:boom");
    expect(payloads).toContain("cancelled");
    expect(payloads).toContain("exec_start");
  });

  it("ephemeral marker kind≠system_marker but payload='exec_start' → survives", () => {
    const fake = item({
      item_id: "_marker.weird",
      kind: "agent_chat",
      payload: "exec_start",
    });
    const result = mergeSnapshotWithStreaming([fake], [], 0);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(fake);
  });

  it("exec_output upserts by id; other ephemeral markers must remain", () => {
    const errorMarker: BackendTimelineItem = {
      item_id: "_marker.err",
      kind: "system_marker",
      source: null,
      payload: "error:test error",
      created_at: "2026-01-01T00:00:00Z",
      inbound_id: null,
      show_timestamp: true,
    };
    const result = applySystemEvent([errorMarker], {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "out",
    });

    expect(result).toHaveLength(2);
    expect(result[0]).toMatchObject({ kind: "system_marker", payload: "error:test error" });
  });
});

describe("applySystemEvent — case label + template literal reinforcement", () => {

  it("exec_output_chunk same id multiple times → payload accumulates by append, not replace (pins case label)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "first",
    });
    items = applySystemEvent(items, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "+second",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "first+second",
      partial: true,
    });
  });

  it("compact_request is a no-op — no visible marker created", () => {
    const result = applySystemEvent([], {
      role: "compact_request",
      agent_id: 1,
      content: "user-typed compact reason",
    });
    expect(result).toEqual([]);
  });

  it("error payload must be 'error:<content>' (pins case label + L277 marker template)", () => {
    const result = applySystemEvent([], {
      role: "error",
      agent_id: 1,
      content: "boom details",
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      kind: "system_marker",
      payload: "error:boom details",
    });

    expect(result[0].item_id.startsWith("_marker.")).toBe(true);
    expect(result[0].item_id).not.toBe("");
  });

  it("renders a permanent provider rejection as a blocked state with its recovery path", () => {
    const result = applySystemEvent([], {
      role: "error",
      agent_id: 1,
      content: "Content Exists Risk",
      error_class: "permanent",
      reason: "bad_request",
      blocked: true,
      recovery: "Choose a different model overlay or resolve the provider policy rejection, then send a new message.",
    } as unknown as SystemEvent);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      kind: "system_marker",
      payload: "error:Provider permanently rejected the request (classification: permanent; reason: bad_request). The agent is blocked and automated heartbeat retries are stopped. Recovery: Choose a different model overlay or resolve the provider policy rejection, then send a new message. Details: Content Exists Risk",
    });
  });
});

describe("mergeSnapshotWithStreaming reference identity reinforcement", () => {
  it("snapshot equal to prev → returns same prev reference (Zustand setState short-circuit)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const b = item({ item_id: "5.1", kind: "agent_chat", payload: "more" });
    const prev = [a, b];
    const snapshot = [a, b];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).toBe(prev);
  });

  it("snapshot has one extra item → must return a new array (reference must flip)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "hi" });
    const prev = [a];
    const snapshot = [a, item({ item_id: "5.1", kind: "agent_chat", payload: "added" })];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result).toHaveLength(2);
  });

  it("same length but one item replaced → returns a new array (pins same-flag actually compares elements)", () => {
    const a = item({ item_id: "5.0", kind: "agent_chat", payload: "old" });
    const a2 = item({ item_id: "5.0", kind: "agent_chat", payload: "new" });
    const prev = [a];
    const snapshot = [a2];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 0);
    expect(result).not.toBe(prev);
    expect(result[0].payload).toBe("new");
  });
});

describe("applySystemEvent no-op case label reinforcement", () => {

  it("token_usage no-op (hook handles it; reducer no-op) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "token_usage",
      agent_id: 1,
      input_tokens: 100,
      output_tokens: 50,
    });
    expect(after).toBe(before);
  });

  it("label_updated no-op (consumed by the useAgentLabelUpdates hook) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "label_updated",
      agent_id: 1,
      label: "new-label",
    });
    expect(after).toBe(before);
  });

  it("timeline_snapshot no-op (store.processSseEvent intercepts; reducer fallback returns items) → returns same prev reference", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "timeline_snapshot",
      agent_id: 1,
      msg_count: 0,
      items: [],
    });
    expect(after).toBe(before);
  });
});

describe("mergeSnapshotWithStreaming — system-prompt keep-verbatim (generic keep rule)", () => {

  it("keeps prev's system-prompt item verbatim when the snapshot omits it", () => {
    const sys = item({ item_id: "0.0", kind: "system_prompt", payload: "BIG PROMPT" });
    const prev = [sys, item({ item_id: "1.0", kind: "agent_chat", payload: "old reply" })];
    const snapshot = [item({ item_id: "1.0", kind: "agent_chat", payload: "old reply" })];
    const result = mergeSnapshotWithStreaming(prev, snapshot, 2);
    expect(result.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
    expect(result[0]).toBe(sys);
  });

  it("keeps prev's system-prompt item even for an empty snapshot", () => {

    const sys = item({ item_id: "0.0", kind: "system_prompt", payload: "BIG PROMPT" });
    const result = mergeSnapshotWithStreaming([sys], [], 1);
    expect(result.map((i) => i.item_id)).toEqual(["0.0"]);
    expect(result[0]).toBe(sys);
  });

});
