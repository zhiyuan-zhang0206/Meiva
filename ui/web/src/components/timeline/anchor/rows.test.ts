import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";

import { identify, locate, rankOf, shiftForCompact, type RowIdentity } from "./rows";

function row(id: string, extra: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return { item_id: id, kind: "agent_chat", payload: `Row ${id}`, source: null, created_at: null, inbound_id: null, show_timestamp: false, ...extra };
}
function buffer(rows: { item: BackendTimelineItem; rank: number }[]): CompactTransitionBuffer {
  return { threadId: 42, epoch: 1, rows: rows.map((entry) => ({ ...entry, needsCanonicalRow: true })), oldestRealByRank: new Map(), tailReady: true };
}

describe("identify", () => {
  it("carries the loaded row's locator and coordinate", () => {
    expect(identify({ itemId: "3.2", rank: 0 }, [row("3.2", { reading_locator: "loc-3" })], null)).toEqual({
      row: { itemId: "3.2", rank: 0 },
      locator: "loc-3",
      coordinate: { rank: 0, msg: 3, block: 2 },
    });
  });

  it("reads a buffered row's coordinate as the compact will place it", () => {
    const buffered = buffer([{ item: row("5.1"), rank: 1 }]);
    expect(identify({ itemId: "5.1", rank: 1 }, [], buffered)).toEqual({
      row: { itemId: "5.1", rank: 1 },
      coordinate: { rank: 1, msg: 4, block: 1 },
    });
  });

  it("keeps only the row when it is not loaded at that rank, or its locator is empty", () => {
    expect(identify({ itemId: "3.0", rank: 1 }, [row("3.0", { reading_locator: "loc-3" })], null)).toEqual({ row: { itemId: "3.0", rank: 1 } });
    expect(identify({ itemId: "3.0", rank: 0 }, [row("3.0", { reading_locator: "" })], null)).toEqual({
      row: { itemId: "3.0", rank: 0 },
      coordinate: { rank: 0, msg: 3, block: 0 },
    });
  });
});

describe("locate", () => {
  const identity = (extra: Partial<RowIdentity> = {}): RowIdentity => ({ row: { itemId: "3.0", rank: 0 }, ...extra });

  it("prefers the exact id and rank over every other match", () => {
    const named = identity({ locator: "loc-elsewhere", coordinate: { rank: 0, msg: 9, block: 0 } });
    const rows = [row("9.0"), row("7.0", { reading_locator: "loc-elsewhere" }), row("3.0")];
    expect(locate(named, rows, null)).toBe(named.row);
  });

  it("then follows the reading locator, ahead of the same id at another rank", () => {
    const sameIdElsewhere = buffer([{ item: row("3.0"), rank: 1 }]);
    const rows = [row("s2.cp.1.0", { reading_locator: "loc-3" })];
    expect(locate(identity({ locator: "loc-3" }), rows, sameIdElsewhere)).toEqual({ itemId: "s2.cp.1.0", rank: 2 });
  });

  it("then the same id at another rank, when only one row has it", () => {
    const buffered = buffer([{ item: row("3.0"), rank: 1 }]);
    expect(locate(identity({ locator: "loc-gone" }), [], buffered)).toEqual({ itemId: "3.0", rank: 1 });
    const twice = buffer([{ item: row("3.0"), rank: 1 }, { item: row("3.0"), rank: 2 }]);
    expect(locate(identity(), [], twice)).toBeNull();
  });

  it("then the compact coordinate", () => {
    const rows = [row("s1.cp.2.0"), row("s1.cp.2.1")];
    expect(locate(identity({ coordinate: { rank: 1, msg: 2, block: 1 } }), rows, null)).toEqual({ itemId: "s1.cp.2.1", rank: 1 });
  });

  it("keeps the whole-turn flag on a relocated row", () => {
    const turn: RowIdentity = { row: { itemId: "3.0", rank: 0, turn: true }, locator: "loc-3" };
    expect(locate(turn, [row("s1.cp.3.0", { reading_locator: "loc-3" })], null)).toEqual({ itemId: "s1.cp.3.0", rank: 1, turn: true });
  });

  it("never lands on a pending or marker row", () => {
    const rows = [row("_pending-1", { reading_locator: "loc-3" }), row("_marker")];
    expect(locate(identity({ locator: "loc-3" }), rows, null)).toBeNull();
    expect(locate({ row: { itemId: "_marker", rank: 0 } }, rows, null)).toBeNull();
  });

  it("answers null when the loaded rows do not hold it", () => {
    expect(locate(identity({ locator: "loc-3", coordinate: { rank: 0, msg: 3, block: 0 } }), [row("4.0"), row("5.0")], null)).toBeNull();
  });
});

describe("shiftForCompact", () => {
  it("moves a current-segment coordinate to where its canonical replacement will sit", () => {
    const before: RowIdentity = { row: { itemId: "3.0", rank: 0 }, coordinate: { rank: 0, msg: 3, block: 0 } };
    const after = shiftForCompact(before, 1);
    expect(after).toEqual({ row: before.row, coordinate: { rank: 1, msg: 2, block: 0 } });
    // meiva:note
    // The replacement row is found by that coordinate once the compact lands.
    expect(locate(after, [row("s1.cp.2.0")], null)).toEqual({ itemId: "s1.cp.2.0", rank: 1 });
  });

  it("shifts only the rank of an older segment", () => {
    const before: RowIdentity = { row: { itemId: "s1.cp.5.2", rank: 1 }, coordinate: { rank: 1, msg: 5, block: 2 } };
    expect(shiftForCompact(before, 1).coordinate).toEqual({ rank: 2, msg: 5, block: 2 });
  });

  it("leaves an identity without a coordinate, or with no shift, unchanged", () => {
    const bare: RowIdentity = { row: { itemId: "3.0", rank: 0 } };
    expect(shiftForCompact(bare, 1)).toBe(bare);
    const placed: RowIdentity = { row: { itemId: "3.0", rank: 0 }, coordinate: { rank: 0, msg: 3, block: 0 } };
    expect(shiftForCompact(placed, 0)).toBe(placed);
  });
});

describe("rankOf", () => {
  it("reads a buffered row's display rank, else the rank in its id", () => {
    const buffered = row("4.0");
    expect(rankOf(buffered, buffer([{ item: buffered, rank: 1 }]))).toBe(1);
    expect(rankOf(row("s2.cp.4.0"), null)).toBe(2);
    expect(rankOf(row("4.0"), null)).toBe(0);
  });
});
