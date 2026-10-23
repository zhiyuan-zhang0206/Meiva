// Timeline reducer unit tests — pure function, no jsdom / RTL.

import { describe, expect, it } from "vitest";

import {
  applySystemEvent,
  parseItemId,
  parseItemIdParts,
  sortByItemId,
  standingHeadNoteIds,
} from "./timeline";
import type { BackendTimelineItem, SystemEvent } from "../contracts/types";

let _id_counter_fix = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    item_id: `fix.${++_id_counter_fix}`,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

function kinds(items: BackendTimelineItem[]): string[] {
  return items.map((i) => i.kind);
}

describe("standingHeadNoteIds", () => {
  it("returns the contiguous system_marker run right after 0.0", () => {
    const items = [
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "system_marker", source: "exec_timeout" }),
      item({ item_id: "2.0", kind: "system_marker", source: "timezone" }),
      item({ item_id: "3.0", kind: "system_marker", source: "memory" }),
      item({ item_id: "4.0", kind: "system_marker", source: "agent_id" }),
      item({ item_id: "5.0", kind: "system_marker", source: "agent_memory" }),
      item({ item_id: "6.0", kind: "inbound_compact_summary" }),
    ];
    expect(standingHeadNoteIds(items)).toEqual(new Set(["1.0", "2.0", "3.0", "4.0", "5.0"]));
  });

  it("stops the run at the first non-marker (compact summary / real item)", () => {
    const items = [
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "system_marker", source: "exec_timeout" }),
      item({ item_id: "2.0", kind: "inbound_compact_summary" }),
      // Mid-conversation markers are NOT head notes — they sit after real items.
      item({ item_id: "3.0", kind: "inbound_chat" }),
      item({ item_id: "4.0", kind: "system_marker", source: "memory" }),
    ];
    expect(standingHeadNoteIds(items)).toEqual(new Set(["1.0"]));
  });

  it("stops the run at an untagged marker (source=null catch-all rows are not head notes)", () => {
    // Pre-tag checkpoint rows / retired markers render as system_marker with
    // source=null via the catch-all. They are stale rows, not standing head
    // notes, and must end the run — otherwise old data would be re-attached
    // to every window and skipped in paging.
    const items = [
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "system_marker", source: "exec_timeout" }),
      item({ item_id: "2.0", kind: "system_marker", source: null, payload: "legacy" }),
      item({ item_id: "3.0", kind: "system_marker", source: "memory" }),
    ];
    expect(standingHeadNoteIds(items)).toEqual(new Set(["1.0"]));
  });

  it("returns an empty set when there is no 0.0 prompt", () => {
    expect(
      standingHeadNoteIds([item({ item_id: "1.0", kind: "system_marker", source: "memory" })]),
    ).toEqual(new Set());
  });

  it("returns an empty set for an empty list", () => {
    expect(standingHeadNoteIds([])).toEqual(new Set());
  });

  it("does not treat historical segment head notes as current head notes", () => {
    const items = [
      item({ item_id: "s2.cpid2.0.0", kind: "system_prompt" }),
      item({ item_id: "s2.cpid2.1.0", kind: "system_marker", source: "exec_timeout" }),
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "system_marker", source: "exec_timeout" }),
      item({ item_id: "2.0", kind: "system_marker", source: "memory" }),
      item({ item_id: "6.0", kind: "inbound_compact_summary" }),
    ];
    expect(standingHeadNoteIds(items)).toEqual(new Set(["1.0", "2.0"]));
  });
});

describe("parseItemId", () => {
  it("standard format '5.1' → [5, 1]", () => {
    expect(parseItemId("5.1")).toEqual([5, 1]);
  });

  it("multi-digit msg_idx '123.456' → [123, 456]", () => {
    expect(parseItemId("123.456")).toEqual([123, 456]);
  });

  it("msg_idx=0 '0.0' → [0, 0]", () => {
    expect(parseItemId("0.0")).toEqual([0, 0]);
  });

  it("ephemeral marker '_marker.xxx' → null", () => {
    expect(parseItemId("_marker.exec_start")).toBeNull();
    expect(parseItemId("_marker.compact_done")).toBeNull();
  });

  it("non-standard format → null", () => {
    expect(parseItemId("not-an-id")).toBeNull();
    expect(parseItemId("")).toBeNull();
    expect(parseItemId("a.b")).toBeNull();
  });

  it("more than three parts '1.2.3' → null", () => {
    expect(parseItemId("1.2.3")).toBeNull();
  });
});

describe("parseItemIdParts", () => {
  it("parses current ids without changing the legacy parser", () => {
    expect(parseItemIdParts("12.3")).toEqual({
      rank: 0,
      checkpointId: null,
      msg: 12,
      block: 3,
    });
    expect(parseItemId("12.3")).toEqual([12, 3]);
  });

  it("parses historical ids with rank and exact checkpoint identity", () => {
    expect(
      parseItemIdParts("s4.1f0b9b12-0000-6000-8000-000000000000.27.2"),
    ).toEqual({
      rank: 4,
      checkpointId: "1f0b9b12-0000-6000-8000-000000000000",
      msg: 27,
      block: 2,
    });
    expect(parseItemId("s4.1f0b9b12-0000-6000-8000-000000000000.27.2")).toBeNull();
  });

  it("rejects malformed and non-positive historical ranks", () => {
    expect(parseItemIdParts("_marker.compact_done")).toBeNull();
    expect(parseItemIdParts("s0.cpid.1.0")).toBeNull();
    expect(parseItemIdParts("s2..1.0")).toBeNull();
    expect(parseItemIdParts("s2.cpid.x.0")).toBeNull();
  });
});

describe("sortByItemId", () => {
  it("standard sort: ascending by msg_idx, then by block_idx within the same msg_idx", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "2.0", kind: "agent_chat", payload: "third" }),
      item({ item_id: "1.0", kind: "inbound_chat", payload: "first" }),
      item({ item_id: "1.1", kind: "agent_reasoning", payload: "second" }),
    ];
    const sorted = sortByItemId(items);
    expect(sorted.map((i) => i.item_id)).toEqual(["1.0", "1.1", "2.0"]);
  });

  it("ephemeral markers at the end; stable items keep msg_idx order", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "_marker.err", kind: "system_marker", payload: "error:x" }),
      item({ item_id: "2.0", kind: "agent_code", payload: "code" }),
      item({ item_id: "_marker.compact", kind: "system_marker", payload: "compact_done" }),
      item({ item_id: "1.0", kind: "inbound_chat", payload: "msg" }),
    ];
    const sorted = sortByItemId(items);
    expect(sorted.map((i) => i.item_id)).toEqual([
      "1.0",
      "2.0",
      "_marker.err",
      "_marker.compact",
    ]);
  });

  it("does not mutate input", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "2.0" }),
      item({ item_id: "1.0" }),
    ];
    const copy = [...items];
    sortByItemId(items);
    expect(items).toEqual(copy);
  });

  it("sorts older segment ranks before newer ranks and current items", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "2.0" }),
      item({ item_id: "s1.newer-checkpoint.3.0" }),
      item({ item_id: "s2.older-checkpoint.5.1" }),
      item({ item_id: "s2.older-checkpoint.5.0" }),
      item({ item_id: "1.0" }),
      item({ item_id: "s1.newer-checkpoint.1.0" }),
    ];

    expect(sortByItemId(items).map((entry) => entry.item_id)).toEqual([
      "s2.older-checkpoint.5.0",
      "s2.older-checkpoint.5.1",
      "s1.newer-checkpoint.1.0",
      "s1.newer-checkpoint.3.0",
      "1.0",
      "2.0",
    ]);
  });
});

describe("applySystemEvent", () => {
  it("code_start appends an empty agent_code entry", () => {
    const ev: SystemEvent = { role: "code_start", item_id: "5.1", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      item_id: "5.1",
      kind: "agent_code",
      payload: "",
    });
  });

  it("code_delta appends payload under the same item_id", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "pri",
    });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "nt(1)",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_code", payload: "print(1)" });
  });

  it("out-of-order new item_ids are insert-sorted, not tail-appended (Task #1051: delta-only path never goes through the snapshot re-sort)", () => {
    let items: BackendTimelineItem[] = [];
    // "3.1" arrives first (e.g. reconnect-window resend), then the earlier
    // "2.5" — a tail append would leave 2.5 AFTER 3.1 until the next
    // snapshot merge.
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "3.1",
      agent_id: 1,
      content: "later",
    });
    items = applySystemEvent(items, {
      role: "chat_delta",
      item_id: "2.5",
      agent_id: 1,
      content: "earlier",
    });
    expect(items.map((i) => i.item_id)).toEqual(["2.5", "3.1"]);
  });

  it("code_delta with a new item_id creates an entry (fallback for missed start, partial)", () => {
    const ev: SystemEvent = {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "x",
    };
    const result = applySystemEvent([], ev);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      item_id: "5.1",
      kind: "agent_code",
      payload: "x",
      partial: true,
    });
  });

  it("reasoning_start + reasoning_delta concatenate into agent_reasoning", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, {
      role: "reasoning_start",
      item_id: "5.0",
      agent_id: 1,
    });
    items = applySystemEvent(items, {
      role: "reasoning_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "Let ",
    });
    items = applySystemEvent(items, {
      role: "reasoning_delta",
      item_id: "5.0",
      agent_id: 1,
      content: "me think",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "agent_reasoning", payload: "Let me think" });
  });

  it("exec_start creates a code_output placeholder with execStartedAt", () => {
    const ev: SystemEvent = { role: "exec_start", item_id: "6.0", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result[0]).toMatchObject({ kind: "code_output", item_id: "6.0", payload: "" });
    expect(result[0].execStartedAt).toBeGreaterThan(0);
  });

  it("exec_output carries a stable item_id, upsert (new id append, same id replace)", () => {
    let items: BackendTimelineItem[] = [
      item({ kind: "agent_code", item_id: "5.1", payload: "print('x')" }),
    ];
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "x\n",
    });
    expect(items).toHaveLength(2);
    expect(items[1]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "x\n",
    });
  });

  it("compact_done is a no-op — no visible marker created", () => {
    const ev: SystemEvent = { role: "compact_done", agent_id: 1 };
    const result = applySystemEvent([], ev);
    expect(result).toEqual([]);
  });

  it("cancelled is a reducer no-op (the store drops the in-flight bubble using msg_count)", () => {
    // The reducer can't tell a committed bubble from a still-streaming one
    // (normal streamed items have `partial` unset). The store does the drop
    // in processSseEvent with msg_count; here the reducer just passes through.
    const items = [item({ item_id: "1.0", kind: "agent_chat", payload: "x" })];
    expect(applySystemEvent(items, { role: "cancelled", agent_id: 1 })).toBe(items);
    expect(applySystemEvent([], { role: "cancelled", agent_id: 1 })).toHaveLength(0);
  });

  it("inbound_arrived no longer produces an optimistic placeholder — no-op; turnActive managed by processSseEvent", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 42,
      kind: "chat",
      source: "user",
      content: "hello from web",
    });
    // optimistic update removed — inbound_arrived inserts no item
    expect(after).toHaveLength(1);
    expect(after[0]).toBe(before[0]);
  });



  it("inbound_committed no-op (hook handles reload; reducer no-op)", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, {
      role: "inbound_committed",
      agent_id: 1,
      inbound_id: 42,
    });
    expect(after).toBe(before);
  });

  it("llm_done no-op (hook handles reload; reducer no-op)", () => {
    const before: BackendTimelineItem[] = [item({ kind: "agent_chat", payload: "hi" })];
    const after = applySystemEvent(before, { role: "llm_done", agent_id: 1 });
    expect(after).toBe(before);
  });


  it("full flow: code_start → delta × 2 → exec_start → exec_output (exec_start creates placeholder)", () => {
    let items: BackendTimelineItem[] = [];
    items = applySystemEvent(items, { role: "code_start", item_id: "5.1", agent_id: 1 });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "a",
    });
    items = applySystemEvent(items, {
      role: "code_delta",
      item_id: "5.1",
      agent_id: 1,
      content: "b",
    });
    items = applySystemEvent(items, { role: "exec_start", item_id: "6.0", agent_id: 1 });
    // exec_start creates a code_output placeholder now
    expect(kinds(items)).toEqual(["agent_code", "code_output"]);
    expect(items[1]).toMatchObject({ kind: "code_output", item_id: "6.0", payload: "" });
    items = applySystemEvent(items, {
      role: "exec_output",
      item_id: "6.0",
      agent_id: 1,
      content: "2",
    });
    // exec_output replaces the placeholder with full content
    expect(kinds(items)).toEqual(["agent_code", "code_output"]);
    expect(items[0]).toMatchObject({ payload: "ab" });
    expect(items[1]).toMatchObject({ kind: "code_output", payload: "2" });
    // code_start cleared reasoning clocks (none present, no-op)
    // exec_start cleared code clocks → code item's codeStartedAt gone
    expect(items[0].codeStartedAt).toBeUndefined();
  });

  it("a keepalive bootstraps an empty live output item after a mid-stream join", () => {
    const items = applySystemEvent([], {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "keepalive payload must not render",
      keepalive: true,
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      item_id: "6.0",
      kind: "code_output",
      payload: "",
      partial: true,
    });
    expect(items[0].execStartedAt).toBeTypeOf("number");
  });

  it("a keepalive for an existing output item preserves the array reference", () => {
    const before = [
      item({ item_id: "6.0", kind: "code_output", payload: "existing", partial: true }),
    ];

    const after = applySystemEvent(before, {
      role: "exec_output_chunk",
      item_id: "6.0",
      agent_id: 1,
      content: "",
      keepalive: true,
    });

    expect(after).toBe(before);
  });
});
