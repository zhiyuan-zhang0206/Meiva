import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "../contracts/types";
import { sortByItemId } from "../fold";
import { findOldestTimelineCursor } from "./timeline-plan";
import { crossedUnseenCompact } from "./timeline-store-events";

function item(item_id: string, overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return {
    item_id,
    kind: "agent_chat",
    source: null,
    payload: item_id,
    created_at: null,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

describe("findOldestTimelineCursor with earlier epochs", () => {
  it("returns the oldest earlier-epoch row, which the next older page continues from", () => {
    const items = sortByItemId([item("0.0", { kind: "system_prompt" }), item("4.0"), item("e0.7.0"), item("e0.8.0")]);
    expect(findOldestTimelineCursor(items)?.item_id).toBe("e0.7.0");
  });

  it("skips an earlier epoch's system prompt but accepts the switch block as a cursor", () => {
    const withPrompt = sortByItemId([item("e0.0.0", { kind: "system_prompt" }), item("e0.1.0"), item("2.0")]);
    expect(findOldestTimelineCursor(withPrompt)?.item_id).toBe("e0.1.0");
    const withSwitch = sortByItemId([item("e1.sw", { kind: "system_marker", source: "harness_switch" }), item("2.0")]);
    expect(findOldestTimelineCursor(withSwitch)?.item_id).toBe("e1.sw");
  });
});

describe("crossedUnseenCompact", () => {
  it("never reads an earlier epoch's compact envelope as an unseen compact", () => {
    const local = [item("h5.0")];
    const incoming = [item("e0.s1.cp.0.0", { kind: "inbound_compact_summary" })];
    expect(crossedUnseenCompact(local, incoming)).toBe(false);
    expect(crossedUnseenCompact(local, [item("s1.cp.0.0", { kind: "inbound_compact_summary" })])).toBe(true);
  });
});
