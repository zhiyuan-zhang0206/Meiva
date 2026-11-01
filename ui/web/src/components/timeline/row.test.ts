// meiva:note
// row.tsx — the per-item card-config cache. Keyed on the item reference so
// memoized rows skip recomputing CardConfig across re-renders while a fresh
// item object computes its own config.

import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import { cardConfigFor } from "./row";

function item(): BackendTimelineItem {
  return {
    item_id: "1.0",
    kind: "agent_code",
    source: null,
    payload: "",
    created_at: "2026-05-15T12:00:00Z",
    inbound_id: null,
    show_timestamp: true,
  };
}

describe("cardConfigFor", () => {
  it("same item reference → the cached config reference", () => {
    const row = item();
    const first = cardConfigFor(row);
    const second = cardConfigFor(row);
    expect(second).toBe(first);
  });

  it("fresh item object → computes config with matching shape", () => {
    const first = cardConfigFor(item());
    const second = cardConfigFor(item());
    expect(second).toEqual(first);
  });
});
