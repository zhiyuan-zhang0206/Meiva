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
    expect(identify({ itemId: "3.2", rank: 0 }, [row("3.2", { reading_locator: "loc-3" })], null)).toMatchObject({
      row: { itemId: "3.2", rank: 0 },
      locator: "loc-3",
      coordinate: { rank: 0, msg: 3, block: 2 },
      content: { kind: "agent_chat" },
    });
  });

  it("reads a buffered row's coordinate as the compact will place it", () => {
    const buffered = buffer([{ item: row("5.1"), rank: 1 }]);
    expect(identify({ itemId: "5.1", rank: 1 }, [], buffered)).toMatchObject({
      row: { itemId: "5.1", rank: 1 },
      coordinate: { rank: 1, msg: 4, block: 1 },
    });
  });

  it("keeps only the row when it is not loaded at that rank, or its locator is empty", () => {
    expect(identify({ itemId: "3.0", rank: 1 }, [row("3.0", { reading_locator: "loc-3" })], null)).toEqual({ row: { itemId: "3.0", rank: 1 } });
    const blank = identify({ itemId: "3.0", rank: 0 }, [row("3.0", { reading_locator: "" })], null);
    expect(blank.locator).toBeUndefined();
    expect(blank.coordinate).toEqual({ rank: 0, msg: 3, block: 0 });
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

  it("never matches an id at another rank: a compact's new segment reuses message numbers", () => {
    const reused = [row("3.0", { payload: "a different message" })];
    expect(locate(identity({ row: { itemId: "3.0", rank: 1 }, content: { kind: "agent_chat", payload: "mine" } }), reused, null)).toBeNull();
  });

  it("then the row's content, nearest to its coordinate when the text repeats", () => {
    const mine = { kind: "agent_chat", payload: "mine" };
    const rows = [row("s1.cp.2.0", { payload: "mine" }), row("s1.cp.9.0", { payload: "mine" }), row("3.0", { payload: "other" })];
    expect(locate(identity({ row: { itemId: "3.0", rank: 1 }, content: mine, coordinate: { rank: 1, msg: 3, block: 0 } }), rows, null))
      .toEqual({ itemId: "s1.cp.2.0", rank: 1 });
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
  it("moves the row and its coordinate one rank older; its content finds the canonical replacement", () => {
    const before: RowIdentity = {
      row: { itemId: "3.0", rank: 0 },
      coordinate: { rank: 0, msg: 3, block: 0 },
      content: { kind: "agent_chat", payload: "mine" },
    };
    const after = shiftForCompact(before, 1);
    expect(after.row).toEqual({ itemId: "3.0", rank: 1 });
    expect(after.coordinate).toEqual({ rank: 1, msg: 3, block: 0 });
    // meiva:note
    // A new rank-0 row reusing the id is not the reader's; the re-keyed one with the same text is.
    const rows = [row("3.0", { payload: "new segment" }), row("s1.cp.3.0", { payload: "mine" })];
    expect(locate(after, rows, null)).toEqual({ itemId: "s1.cp.3.0", rank: 1 });
  });

  it("leaves an identity unchanged when there is no shift", () => {
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
