// meiva:note
// A timeline read from a CLI's own record names its items `h<byte offset>.<n>`: large, sparse, ordered integers
// (the file only grows, so an id is the same however the row was reached). These tests lock down that the page
// treats such ids like any other harness id: it orders them numerically, merges a snapshot by id, replaces a running
// call in place, and does not read a live snapshot that continues what it holds as a gap in it.

import { describe, expect, it } from "vitest";

import { foldEvent, parseItemIdParts, sortByItemId } from "../fold/timeline";
import type { ThreadTimelineState } from "../fold/timeline";
import type { BackendTimelineItem } from "../contracts/types";
import { hasDisjointSnapshotGap } from "./timeline-store-events";

function item(id: string, overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: id,
    created_at: "2026-11-12T08:56:00+00:00",
    item_id: id,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

const HELD = [item("h413.0"), item("h2620.0"), item("h4358.0", { kind: "agent_code" })];

describe("items named by byte offsets", () => {
  it("parse and sort numerically, past the sizes a sequence number reaches", () => {
    const ids = ["h12000000.0", "h413.1", "h413.0", "h99999.0"];
    expect(parseItemIdParts("h12000000.0")).not.toBeNull();
    expect(sortByItemId(ids.map((id) => item(id))).map((it) => it.item_id)).toEqual([
      "h413.0",
      "h413.1",
      "h99999.0",
      "h12000000.0",
    ]);
  });

  it("a snapshot that starts at the last item the page holds is not a gap in it", () => {
    const bridged = [HELD[2], item("h6222.0", { kind: "code_output" })];
    expect(hasDisjointSnapshotGap(HELD, bridged, true)).toBe(false);
  });

  it("a snapshot that overlaps the page's window is not a gap either", () => {
    const window = [HELD[1], HELD[2], item("h6222.0", { kind: "code_output" })];
    expect(hasDisjointSnapshotGap(HELD, window, true)).toBe(false);
  });

  it("a snapshot that starts after everything the page holds is a gap, which the carried item avoids", () => {
    expect(hasDisjointSnapshotGap(HELD, [item("h6222.0")], true)).toBe(true);
  });

  it("a live snapshot merges by id, and the call that was running is replaced in place", () => {
    const running = item("h4358.0", { kind: "agent_code", activity_pending: true, activity_revision: 1 });
    const state = { items: [HELD[0], HELD[1], running], streamingIds: new Set<string>() } as unknown as ThreadTimelineState;
    const settled = item("h4358.0", { kind: "agent_code", activity_pending: null, activity_revision: null });
    const result = foldEvent(state, {
      role: "timeline_snapshot",
      agent_id: 1,
      items: [settled, item("h6222.0", { kind: "code_output" })],
      msg_count: 9800,
    } as never);
    expect(result.items.map((it) => it.item_id)).toEqual(["h413.0", "h2620.0", "h4358.0", "h6222.0"]);
    expect(result.items[2].activity_pending).toBeNull();
  });

  it("a running call a snapshot does not mention is dropped, so a snapshot carries every running call", () => {
    const running = item("h4358.0", { kind: "agent_code", activity_pending: true, activity_revision: 1 });
    const state = { items: [HELD[0], running], streamingIds: new Set<string>() } as unknown as ThreadTimelineState;
    const result = foldEvent(state, {
      role: "timeline_snapshot",
      agent_id: 1,
      items: [item("h6222.0")],
      msg_count: 9800,
    } as never);
    expect(result.items.map((it) => it.item_id)).toEqual(["h413.0", "h6222.0"]);
    const carried = foldEvent(state, {
      role: "timeline_snapshot",
      agent_id: 1,
      items: [running, item("h6222.0")],
      msg_count: 9800,
    } as never);
    expect(carried.items.map((it) => it.item_id)).toEqual(["h413.0", "h4358.0", "h6222.0"]);
  });
});
