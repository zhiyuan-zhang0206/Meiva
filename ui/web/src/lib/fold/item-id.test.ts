import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "../contracts/types";
import { isEpochItemId, isSyntheticEpochItemId, parseEpochItemId } from "./epoch-id";
import {
  parseItemId,
  parseItemIdParts,
  sortByItemId,
} from "./item-id";

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

const ids = (items: readonly BackendTimelineItem[]) => items.map((it) => it.item_id);

describe("epoch-prefixed item ids", () => {
  it("parses the prefix and leaves the local id to the existing grammar", () => {
    expect(parseEpochItemId("e0.h123.0")).toEqual({ epoch: 0, local: "h123.0" });
    expect(parseEpochItemId("e12.s1.cp.3.0")).toEqual({ epoch: 12, local: "s1.cp.3.0" });
    expect(parseEpochItemId("e2.sw")).toEqual({ epoch: 2, local: "sw" });
    expect(parseItemIdParts("s1.cp.3.0")).toEqual({ rank: 1, checkpointId: "cp", msg: 3, block: 0 });
  });

  it("does not mistake current-epoch or display-only ids for epoch ids", () => {
    for (const id of ["h123.0", "5.0", "s1.cp.3.0", "_marker.1", "live.abc", "e.0", "ex.1.0", "e0"]) {
      expect(isEpochItemId(id)).toBe(false);
    }
    expect(isSyntheticEpochItemId("e1.sw")).toBe(true);
    expect(isSyntheticEpochItemId("e1.unavailable")).toBe(false);
    expect(isSyntheticEpochItemId("e1.5.0")).toBe(false);
  });

  it("stays opaque to the current-epoch parsers so compact logic never sees it", () => {
    expect(parseItemId("e0.h123.0")).toBeNull();
    expect(parseItemIdParts("e0.h123.0")).toBeNull();
    expect(parseItemIdParts("e0.s1.cp.3.0")).toBeNull();
  });
});

describe("sortByItemId across epochs", () => {
  it("orders meiva -> claude_code -> codex -> meiva by (epoch, local order), ignoring created_at", () => {
    const shuffled = [
      item("3.0", { created_at: "2026-01-01T00:00:00Z" }),
      item("0.0", { kind: "system_prompt" }),
      item("e2.h900.1", { created_at: "2026-12-01T00:00:00Z" }),
      item("e1.h100.0", { created_at: "2026-12-01T00:00:00Z" }),
      item("e0.2.0", { created_at: "2026-12-01T00:00:00Z" }),
      item("e1.sw", { kind: "system_marker", source: "harness_switch" }),
      item("e2.sw", { kind: "system_marker", source: "harness_switch" }),
      item("e0.1.0"),
      item("e3.sw", { kind: "system_marker", source: "harness_switch" }),
      item("e1.h50.0"),
      item("e2.h900.0"),
    ];
    expect(ids(sortByItemId(shuffled))).toEqual([
      "e0.1.0",
      "e0.2.0",
      "e1.sw",
      "e1.h50.0",
      "e1.h100.0",
      "e2.sw",
      "e2.h900.0",
      "e2.h900.1",
      "e3.sw",
      "0.0",
      "3.0",
    ]);
  });

  it("puts the switch block before the rest of its epoch", () => {
    const sorted = sortByItemId([
      item("e1.4.0"),
      item("e1.sw", { kind: "system_marker", source: "harness_switch" }),
      item("e0.9.0"),
    ]);
    expect(ids(sorted)).toEqual(["e0.9.0", "e1.sw", "e1.4.0"]);
  });

  it("keeps compact segments of an earlier epoch oldest-first, as in the current epoch", () => {
    const sorted = sortByItemId([item("e0.0.0"), item("e0.s2.cpB.1.0"), item("e0.s1.cpA.4.0")]);
    expect(ids(sorted)).toEqual(["e0.s2.cpB.1.0", "e0.s1.cpA.4.0", "e0.0.0"]);
  });

  it("leaves display-only ids at the end in their input order", () => {
    const sorted = sortByItemId([item("_marker.b"), item("e0.1.0"), item("live.x"), item("5.0")]);
    expect(ids(sorted)).toEqual(["e0.1.0", "5.0", "_marker.b", "live.x"]);
  });
});
