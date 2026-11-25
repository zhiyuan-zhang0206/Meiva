import { describe, expect, it, vi } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";
import type { BackendTimelineItem } from "@/lib/contracts/types";

import { readReloadBookmark, saveReloadBookmark, type ReloadRestoreConfig } from "../model/reading-position";
import { READING_TOP_PX, TAIL, type ViewAnchor } from "./dom";
import { initialGoal, PlacementMachine, type PlacementEffects, type PlacementInputs, type SearchBounds } from "./placement";
import type { SavedPlace, TailDeparture } from "./saved";
import { tailDeparture } from "./unread";

const BOUNDS: SearchBounds = { maxPages: 5, maxItems: 250, timeoutMs: 10_000 };

function row(id: string, extra: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return { item_id: id, kind: "agent_chat", payload: `Row ${id}`, source: null, created_at: null, inbound_id: null, show_timestamp: false, ...extra };
}
const rows = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => row(`${from + index}.0`, { reading_locator: `loc-${from + index}` }));

function inputs(overrides: Partial<PlacementInputs> = {}): PlacementInputs {
  return {
    threadKey: "42",
    reloadRestore: { state: "disabled" },
    items: rows(1, 8),
    compactBuffer: null,
    loading: false,
    hasMoreOlder: false,
    loadingOlder: false,
    currentEpoch: null,
    canLoadOlder: true,
    reveal: { decided: true, itemId: null },
    now: 0,
    ...overrides,
  };
}

function effects() {
  return {
    place: vi.fn<PlacementEffects["place"]>(),
    loadOlder: vi.fn<PlacementEffects["loadOlder"]>(),
    cancelOlder: vi.fn<PlacementEffects["cancelOlder"]>(),
    dropSessionPlace: vi.fn<PlacementEffects["dropSessionPlace"]>(),
  };
}

const place = (id: string, offset = -40, extra: Partial<SavedPlace> = {}): SavedPlace => ({
  identity: { row: { itemId: id, rank: 0 } },
  offset,
  ...extra,
});
const at = (id: string, offset: number, rank = 0): ViewAnchor => ({ kind: "row", row: { itemId: id, rank }, offset });

function sessionWith(id: string, extra: { readingLocator?: string; turn?: true; viewportTop?: number; epoch?: number } = {}) {
  const browser = fakeBrowser({ session: {} });
  saveReloadBookmark(browser.session, "42", { itemId: id, rank: 0, viewportTop: extra.viewportTop ?? -60, scrollTop: 300, savedAt: 1000, ...extra }, 1000);
  const config: ReloadRestoreConfig = { state: "enabled", session: browser.session, now: () => 2000 };
  return { session: browser.session, config };
}

function machineFor(memory: SavedPlace | TailDeparture | null, config: ReloadRestoreConfig = { state: "disabled" }) {
  return new PlacementMachine(initialGoal(memory, "42", config), BOUNDS);
}

describe("opening goal", () => {
  it("prefers the memory place over the reload bookmark", () => {
    const { config } = sessionWith("2.0");
    const machine = machineFor(place("6.0", 0), config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config }), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(at("6.0", 0));
  });

  it("opens the reload bookmark when there is no memory place", () => {
    const { config } = sessionWith("2.0", { viewportTop: -60 });
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("2.0", -60));
  });

  it("opens the tail for a view with no thread", () => {
    const machine = new PlacementMachine(initialGoal(null, undefined, { state: "disabled" }), BOUNDS);
    const fx = effects();
    machine.step(inputs({ threadKey: undefined, reveal: { decided: false, itemId: null } }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(machine.snapshot().visible).toBe(true);
  });

  it("stays hidden while the reload settings load, then opens their bookmark", () => {
    const { config } = sessionWith("3.0", { viewportTop: 10 });
    const machine = machineFor(null, { state: "loading" });
    const fx = effects();
    machine.step(inputs({ reloadRestore: { state: "loading" } }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(machine.snapshot().visible).toBe(false);
    machine.step(inputs({ reloadRestore: config }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("3.0", 10));
    expect(machine.snapshot().visible).toBe(true);
  });

  it("falls through to the first-visit decision once settings load without a bookmark", () => {
    const machine = machineFor(null, { state: "loading" });
    const fx = effects();
    machine.step(inputs({ reloadRestore: { state: "loading" }, reveal: { decided: false, itemId: null } }), fx);
    machine.step(inputs({ reloadRestore: { state: "disabled" }, reveal: { decided: false, itemId: null } }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs({ reveal: { decided: true, itemId: null } }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });
});

describe("first visit", () => {
  it("keeps rows hidden until the reveal is decided, then opens the tail", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ reveal: { decided: false, itemId: null } }), fx);
    expect(machine.snapshot().visible).toBe(false);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs(), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(machine.snapshot().visible).toBe(true);
  });

  it("waits for the first rows while the read is loading", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ items: [], loading: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(machine.snapshot().visible).toBe(false);
    machine.step(inputs({ loading: true }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("opens with the first unread row as the tail bound", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ reveal: { decided: true, itemId: "5.0" } }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "5.0", rank: 0 } });
    expect(machine.snapshot()).toMatchObject({ visible: true, pin: { itemId: "5.0", rank: 0 } });
  });

  it("opens the tail when the revealed row is not loaded", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ hasMoreOlder: true, reveal: { decided: true, itemId: "99.0" } }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(fx.loadOlder).not.toHaveBeenCalled();
  });

  it("keeps a short first page hidden while the cold fill reads older pages, then opens the tail", () => {
    const machine = machineFor(null);
    const fx = effects();
    let filling = true;
    machine.step(inputs({ filling: () => filling }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(machine.snapshot().visible).toBe(false);
    expect(machine.snapshot().fillAllowed).toBe(true);
    filling = false;
    machine.step(inputs({ filling: () => filling, now: 1 }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(machine.snapshot().visible).toBe(true);
  });

  it("waits for an undecided reveal within the time bound, then opens the tail", () => {
    const machine = machineFor(null);
    const fx = effects();
    const undecided = { decided: false, itemId: null };
    machine.step(inputs({ items: [], loading: true, reveal: undecided, now: 0 }), fx);
    expect(machine.snapshot()).toEqual({ visible: false, searching: false, pin: null, fillAllowed: true });
    // meiva:note
    // The bound runs from the moment the rows are in, not from the mount.
    machine.step(inputs({ reveal: undecided, now: 5_000 }), fx);
    expect(machine.snapshot()).toEqual({ visible: false, searching: true, pin: null, fillAllowed: true });
    machine.step(inputs({ reveal: undecided, now: 5_000 + BOUNDS.timeoutMs - 1 }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs({ reveal: undecided, now: 5_000 + BOUNDS.timeoutMs }), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(machine.snapshot()).toEqual({ visible: true, searching: false, pin: null, fillAllowed: true });
    // meiva:note
    // A decision that arrives after the bound no longer moves the reader.
    machine.step(inputs({ reveal: { decided: true, itemId: "5.0" }, now: 5_000 + BOUNDS.timeoutMs + 1 }), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
  });

  it("lands a reveal decided inside the bound", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ reveal: { decided: false, itemId: null }, now: 0 }), fx);
    machine.step(inputs({ reveal: { decided: true, itemId: "5.0" }, now: BOUNDS.timeoutMs - 1 }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "5.0", rank: 0 } });
    expect(machine.snapshot().searching).toBe(false);
  });

  it("loses to a saved place: a reveal never moves a returning reader", () => {
    const machine = machineFor(place("3.0", -20));
    const fx = effects();
    machine.step(inputs({ reveal: { decided: true, itemId: "7.0" } }), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(at("3.0", -20));
  });
});

describe("unread boundary", () => {
  const human = row("1.0", { kind: "inbound_chat", source: "user" });
  const work = row("2.0", { kind: "agent_reasoning" });
  const reply = row("4.0");
  const showReply = { decided: true, itemId: reply.item_id };

  function delivered(item: BackendTimelineItem, id: number): BackendTimelineItem {
    return {
      ...item,
      userDelivery: {
        id, agent_id: 42, mode: "prefix", source_key: `harness:${item.item_id}`,
        content: item.payload, suggestions: [], created_at: "2026-11-10T00:00:00Z", seen: false,
      },
    };
  }

  it("includes the adjacent human prompt across ordinary work within the same turn", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ items: [human, work, reply], reveal: showReply }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "1.0", rank: 0 } });
  });

  it.each([
    row("3.0"),
    row("3.0", { kind: "inbound_chat", source: "agent:7" }),
    row("3.0", { kind: "inbound_chat", source: "schedule:2" }),
    row("3.0", { kind: "system_marker", source: "non_human_wake" }),
    row("3.0", { kind: "system_marker", source: "lifecycle_restart" }),
  ])("keeps the reply as the boundary after a prior chat or wake (%o)", (boundary) => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ items: [human, work, boundary, reply], reveal: showReply }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "4.0", rank: 0 } });
  });

  it("stops prompt association at a real compact cursor in an older rank", () => {
    const machine = machineFor(null);
    const fx = effects();
    const archived = [
      { ...human, item_id: "s1.cp.1.0" },
      row("s1.cp.2.0", { kind: "inbound_compact_summary" }),
      { ...work, item_id: "s1.cp.3.0" },
      { ...reply, item_id: "s1.cp.4.0" },
    ];
    machine.step(inputs({ items: archived, reveal: { decided: true, itemId: "s1.cp.4.0" }, hasMoreOlder: true }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "s1.cp.4.0", rank: 1 } });
  });

  it("waits for qualified rows on a tail return, then includes the new unpublished reply", () => {
    const machine = machineFor(tailDeparture([human, work], null));
    const fx = effects();
    machine.step(inputs({ items: [human, work], loading: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs({ items: [human, work, { ...reply, partial: true }] }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "1.0", rank: 0 } });
  });

  it.each(["2.0", "3.0"])("keeps the tail when the feed delivers an unchanged departure row or earlier reply (%s)", (itemId) => {
    const old = rows(1, 3);
    const machine = machineFor(tailDeparture(old, null));
    const fx = effects();
    const reveal = { decided: true, itemId, entrySeenId: 0 };
    machine.step(inputs({ items: old.map((item) => item.item_id === itemId ? delivered(item, 1) : item), reveal }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("opens on the first new unread reply when an older delivery leads the feed", () => {
    const old = rows(1, 3);
    const machine = machineFor(tailDeparture(old, null));
    const fx = effects();
    const items = [delivered(old[0], 6), ...old.slice(1), delivered(reply, 5), delivered(row("5.0"), 7)];
    const reveal = { decided: true, itemId: old[0].item_id, entrySeenId: 5 };
    machine.step(inputs({ items, reveal }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "5.0", rank: 0 } });
  });

  it.each([false, true])("ignores an old delivery after archiving the departure row (new reply: %s)", (hasNewReply) => {
    const old = rows(101, 102);
    const locator = (message_id: string, checkpoint_id?: string) => btoa(JSON.stringify({
      version: 1, source: "native", agent_id: 42, epoch: "initial", checkpoint_id, message_id, block: 0,
    })).replace(/=+$/, "");
    const before = old.map((item) => ({ ...item, reading_locator: locator(item.item_id) }));
    const [earlier, watermark] = before.map((item) => ({
      ...item, item_id: `s1.boundary.${item.item_id}`, reading_locator: locator(item.item_id, "boundary"),
    }));
    const machine = machineFor(tailDeparture(before, null));
    const fx = effects();
    const archived = [delivered(earlier, 1), watermark];
    const items = hasNewReply ? [...archived, human, delivered(reply, 2)] : archived;
    machine.step(inputs({ items, reveal: { decided: true, itemId: earlier.item_id, entrySeenId: 0 } }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.place).toHaveBeenCalledWith(hasNewReply
      ? { kind: "bounded-tail", row: { itemId: human.item_id, rank: 0 } }
      : TAIL);
  });

  it.each(["published", "unpublished", "published while paging"])("reads the prompt across a page boundary (%s)", (publication) => {
    const machine = machineFor(tailDeparture([human], null));
    const fx = effects();
    const reveal = publication === "published" ? showReply : { decided: true, itemId: null, entrySeenId: 0 };
    const latest = { items: [work, reply], hasMoreOlder: true, reveal };
    machine.step(inputs(latest), fx);
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(machine.snapshot()).toMatchObject({ visible: false, fillAllowed: false });
    const finalReply = publication === "published while paging" ? delivered(reply, 2) : reply;
    machine.step(inputs({ ...latest, items: [work, finalReply], loadingOlder: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs({ ...latest, items: [human, work, finalReply] }), fx);
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "1.0", rank: 0 } });
  });

  it("keeps the first loaded reply visible when the unread search expires without a watermark", () => {
    const machine = machineFor(tailDeparture([human], null));
    const fx = effects();
    const latest = { items: [work, reply], hasMoreOlder: true };
    machine.step(inputs(latest), fx);
    machine.step(inputs({ ...latest, now: BOUNDS.timeoutMs, loadingOlder: true }), fx);
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(fx.cancelOlder).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "4.0", rank: 0 } });
  });

  it("shares the older-page limit with the delivery lookup", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs({ items: [work, reply], hasMoreOlder: true, reveal: { ...showReply, olderPages: BOUNDS.maxPages } }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "4.0", rank: 0 } });
  });

  it("keeps the boundary when a reply visible at departure grew before the return", () => {
    const machine = machineFor(tailDeparture([human, { ...reply, partial: true, payload: "Row" }], null));
    const fx = effects();
    machine.step(inputs({ items: [human, { ...reply, partial: true }] }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: "1.0", rank: 0 } });
  });

  it("opens on the first unread reply when compaction reused the partial departure's row id", () => {
    const partial = row("6.0", { partial: true, payload: "Short start", source_message_id: "departure" });
    const archived = { ...partial, item_id: "s1.boundary.5.0", partial: false, payload: "Completed before compaction", source_block_idx: 0 };
    const reused = row("6.0", { source_message_id: "different-reply", source_block_idx: 0 });
    const secondPrompt = { ...human, item_id: "5.0" };
    const machine = machineFor(tailDeparture([partial], null));
    const fx = effects();
    machine.step(inputs({
      items: [archived, human, work, delivered(reply, 1), secondPrompt, delivered(reused, 2)],
      reveal: { ...showReply, entrySeenId: 0 },
    }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: human.item_id, rank: 0 } });
  });

  it.each([false, true])("recognizes an archived streaming watermark whose delivery arrived later (new reply: %s)", (hasNewReply) => {
    const partial = row("6.1", { partial: true, payload: "Full reply text", source_message_id: "departure" });
    const otherBlock = row("s1.boundary.5.0", { source_message_id: "departure", source_block_idx: 0 });
    const archived = delivered({ ...partial, item_id: "s1.boundary.5.1", partial: false, source_block_idx: 1 }, 1);
    const machine = machineFor(tailDeparture([partial], null));
    const fx = effects();
    const items = hasNewReply ? [otherBlock, archived, human, work, delivered(reply, 2)] : [otherBlock, archived];
    machine.step(inputs({ items, reveal: { decided: true, itemId: archived.item_id, entrySeenId: 0 } }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.place).toHaveBeenCalledWith(hasNewReply
      ? { kind: "bounded-tail", row: { itemId: human.item_id, rank: 0 } }
      : TAIL);
  });

  it.each([
    { suffix: " with new content", opensAtPrompt: true },
    { suffix: "  \n", opensAtPrompt: false },
  ])("uses the archived prompt only if the same streaming block appended content (%o)", ({ suffix, opensAtPrompt }) => {
    const partial = row("6.0", { partial: true, payload: "Short start", source_message_id: "departure" });
    const archivedPrompt = { ...human, item_id: "s1.boundary.4.0" };
    const archived = delivered({
      ...partial, item_id: "s1.boundary.5.0", partial: false, source_block_idx: 0, payload: partial.payload + suffix,
    }, 1);
    const machine = machineFor(tailDeparture([human, partial], null));
    const fx = effects();
    machine.step(inputs({
      items: [archivedPrompt, archived],
      reveal: { decided: true, itemId: archived.item_id, entrySeenId: 0 },
    }), fx);
    expect(fx.place).toHaveBeenCalledWith(opensAtPrompt
      ? { kind: "bounded-tail", row: { itemId: archivedPrompt.item_id, rank: 1 } }
      : TAIL);
  });

  it("recognizes the same streaming block when it finalizes and an older delivery leads the feed", () => {
    const older = delivered(row("0.0"), 1);
    const partial = { ...reply, source_message_id: "departure", partial: true, payload: "Short start" };
    const finalReply = delivered({
      ...reply, source_message_id: "departure", source_block_idx: 0,
      reading_locator: "loc-final", payload: partial.payload + " with new content",
    }, 2);
    const machine = machineFor(tailDeparture([older, human, work, partial], null));
    const fx = effects();
    machine.step(inputs({
      items: [older, human, work, finalReply],
      reveal: { ...showReply, itemId: older.item_id, entrySeenId: 0 },
    }), fx);
    expect(fx.place).toHaveBeenCalledWith({ kind: "bounded-tail", row: { itemId: human.item_id, rank: 0 } });
  });
});

describe("memory place", () => {
  it("lands on a retained row at once, even while the authority read is loading", () => {
    const machine = machineFor(place("4.0", -100));
    const fx = effects();
    machine.step(inputs({ loading: true, reveal: { decided: false, itemId: null } }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("4.0", -100));
    expect(machine.snapshot().visible).toBe(true);
  });

  it("pins the goal row before the first pass places it", () => {
    const machine = machineFor(place("4.0"));
    expect(machine.goalRow(inputs())).toEqual({ itemId: "4.0", rank: 0 });
  });

  it("waits hidden, without paging, while a loading read may still bring the row", () => {
    const machine = machineFor(place("40.0"));
    const fx = effects();
    machine.step(inputs({ loading: true, hasMoreOlder: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(machine.snapshot()).toEqual({ visible: false, searching: false, pin: null, fillAllowed: false });
    machine.step(inputs({ items: rows(30, 45) }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("40.0", -40));
  });

  it("bounds the wait from the first pass that could not place the row, even behind another older read", () => {
    const { config } = sessionWith("2.0");
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config, items: rows(30, 45), hasMoreOlder: true, loadingOlder: true, now: 1_000 }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(machine.snapshot().searching).toBe(true);
    machine.step(inputs({ reloadRestore: config, items: rows(30, 45), hasMoreOlder: true, loadingOlder: true, now: 1_000 + BOUNDS.timeoutMs }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(fx.dropSessionPlace).toHaveBeenCalled();
  });

  it("falls back to the tail without touching the reload bookmark when the row is gone", () => {
    const { config, session } = sessionWith("2.0");
    const machine = machineFor(place("99.0"), config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(fx.dropSessionPlace).not.toHaveBeenCalled();
    expect(readReloadBookmark(session, "42", 2000)?.itemId).toBe("2.0");
  });
});

describe("reload bookmark search", () => {
  const start = (target = "2.0") => {
    const { config } = sessionWith(target);
    const machine = machineFor(null, config);
    const fx = effects();
    let items = rows(13, 16);
    const pass = (overrides: Partial<PlacementInputs> = {}) =>
      machine.step(inputs({ reloadRestore: config, items, hasMoreOlder: true, ...overrides }), fx);
    const prepend = (from: number, to: number) => { items = [...rows(from, to), ...items]; };
    return { machine, fx, pass, prepend, items: () => items };
  };

  it("reads older pages while hidden until the row arrives", () => {
    const { machine, fx, pass, prepend } = start();
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(machine.snapshot()).toMatchObject({ visible: false, searching: true });
    pass({ loadingOlder: true });
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    prepend(9, 12);
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(2);
    expect(machine.snapshot().visible).toBe(false);
    pass({ loadingOlder: true });
    prepend(1, 8);
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(2);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(at("2.0", -60));
    expect(fx.cancelOlder).not.toHaveBeenCalled();
    expect(fx.dropSessionPlace).not.toHaveBeenCalled();
    expect(machine.snapshot()).toMatchObject({ visible: true, searching: false });
  });

  it("finds the row by its reading locator when the page names it by another id", () => {
    const { config } = sessionWith("2.0", { readingLocator: "loc-old-2" });
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config, items: rows(13, 16), hasMoreOlder: true }), fx);
    machine.step(inputs({ reloadRestore: config, items: [row("s1.cp.2.0", { reading_locator: "loc-old-2" }), ...rows(13, 16)], hasMoreOlder: true }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("s1.cp.2.0", -60, 1));
  });

  it("does not ask again while a requested page has not started", () => {
    const { fx, pass } = start();
    pass();
    pass();
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(fx.place).not.toHaveBeenCalled();
  });

  it("gives up when a page ends without reaching further back", () => {
    const { machine, fx, pass } = start();
    pass();
    pass({ loadingOlder: true });
    pass();
    expect(fx.cancelOlder).toHaveBeenCalledTimes(1);
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(machine.snapshot().visible).toBe(true);
  });

  it("ignores rows appended at the tail while a page is in flight", () => {
    const { fx, pass, items } = start();
    pass();
    pass({ loadingOlder: true, items: [...items(), ...rows(17, 30)] });
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(fx.place).not.toHaveBeenCalled();
  });

  it("stops after the page bound and opens the tail", () => {
    const { fx, pass, prepend } = start("1.0");
    pass();
    for (let page = 0; page < BOUNDS.maxPages; page++) {
      pass({ loadingOlder: true });
      prepend(-10 * (page + 1) + 100, -10 * (page + 1) + 109);
      pass();
    }
    expect(fx.loadOlder).toHaveBeenCalledTimes(BOUNDS.maxPages);
    expect(fx.cancelOlder).toHaveBeenCalledTimes(1);
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("stops after the item bound and opens the tail", () => {
    const { fx, pass, prepend } = start("1.0");
    pass();
    pass({ loadingOlder: true });
    prepend(1000, 1000 + BOUNDS.maxItems - 1);
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("stops at the time bound, cancelling the page in flight", () => {
    const { fx, pass } = start();
    pass({ now: 100 });
    pass({ now: 100 + BOUNDS.timeoutMs - 1, loadingOlder: true });
    expect(fx.place).not.toHaveBeenCalled();
    pass({ now: 100 + BOUNDS.timeoutMs, loadingOlder: true });
    expect(fx.cancelOlder).toHaveBeenCalledTimes(1);
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("opens the tail at once when there is no older history to read", () => {
    const { config } = sessionWith("2.0");
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config, items: rows(13, 16), hasMoreOlder: false }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.cancelOlder).not.toHaveBeenCalled();
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("opens the tail at once when the view cannot read older pages", () => {
    const { config } = sessionWith("2.0");
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config, items: rows(13, 16), hasMoreOlder: true, canLoadOlder: false }), fx);
    expect(fx.loadOlder).not.toHaveBeenCalled();
    expect(fx.dropSessionPlace).toHaveBeenCalledTimes(1);
  });

  it("waits for another reader-held page before starting its own", () => {
    const { fx, pass } = start();
    pass({ loadingOlder: true });
    expect(fx.loadOlder).not.toHaveBeenCalled();
    pass();
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
  });
});

describe("a saved place across a harness switch", () => {
  const earlier = (...ids: string[]) => ids.map((id) => row(id, { reading_locator: undefined }));

  it("finds a place saved in an epoch that has since ended by its epoch-prefixed id", () => {
    const machine = machineFor(place("12.0", -25, { epoch: 2 }));
    const fx = effects();
    machine.step(inputs({ currentEpoch: 3, items: [...earlier("e2.11.0", "e2.12.0"), ...rows(40, 42)] }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("e2.12.0", -25));
  });

  it("keeps the exact id while the epoch has not changed", () => {
    const machine = machineFor(place("12.0", -25, { epoch: 3 }));
    const fx = effects();
    machine.step(inputs({ currentEpoch: 3, items: [...earlier("e2.12.0"), ...rows(11, 13)] }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("12.0", -25));
  });

  it("does not relabel without a known current epoch", () => {
    const machine = machineFor(place("12.0", -25, { epoch: 2 }));
    const fx = effects();
    machine.step(inputs({ currentEpoch: null, items: [...earlier("e2.12.0"), ...rows(40, 42)], hasMoreOlder: false }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("reads older pages for the relabelled row and falls back to the tail when it never arrives", () => {
    const machine = machineFor(place("12.0", -25, { epoch: 2 }));
    const fx = effects();
    machine.step(inputs({ currentEpoch: 3, items: rows(40, 42), hasMoreOlder: true }), fx);
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    machine.step(inputs({ currentEpoch: 3, items: [...earlier("e2.1.0"), ...rows(40, 42)], hasMoreOlder: false }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
  });

  it("leaves a place that already names an earlier epoch untouched", () => {
    const machine = machineFor(place("e1.7.0", -10, { epoch: 2 }));
    const fx = effects();
    machine.step(inputs({ currentEpoch: 3, items: [...earlier("e1.7.0"), ...rows(40, 42)] }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("e1.7.0", -10));
  });

  it("stores the epoch with a reload bookmark and reads it back", () => {
    const { session, config } = sessionWith("12.0", { epoch: 2 });
    saveReloadBookmark(session, "42", { itemId: "12.0", rank: 0, epoch: 2, viewportTop: -5, scrollTop: 9, savedAt: 1000 }, 1000);
    const machine = machineFor(null, config);
    const fx = effects();
    machine.step(inputs({ reloadRestore: config, currentEpoch: 3, items: [...earlier("e2.12.0"), ...rows(40, 42)] }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("e2.12.0", -5));
  });
});

describe("requested placements", () => {
  it("land only once the opening place is reached", () => {
    const machine = machineFor(place("40.0"));
    const fx = effects();
    machine.request(at("5.0", READING_TOP_PX), inputs({ loading: true }));
    expect(machine.snapshot().pin).toEqual({ itemId: "5.0", rank: 0 });
    machine.step(inputs({ loading: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    machine.step(inputs({ items: rows(1, 45) }), fx);
    expect(fx.place.mock.calls).toEqual([
      [at("40.0", -40)],
      [at("5.0", READING_TOP_PX), { smooth: false }],
    ]);
  });

  it("land once, with their smooth flag, and keep their row pinned until the reader moves", () => {
    const machine = machineFor(null);
    const fx = effects();
    machine.step(inputs(), fx);
    fx.place.mockClear();
    machine.request(TAIL, inputs(), { smooth: true });
    machine.step(inputs(), fx);
    machine.step(inputs(), fx);
    expect(fx.place).toHaveBeenCalledTimes(1);
    expect(fx.place).toHaveBeenCalledWith(TAIL, { smooth: true });

    machine.request(at("3.0", READING_TOP_PX), inputs());
    machine.step(inputs(), fx);
    expect(machine.snapshot().pin).toEqual({ itemId: "3.0", rank: 0 });
    machine.readerMoved(inputs(), null);
    expect(machine.snapshot().pin).toBeNull();
  });
});

describe("a placed row that leaves the loaded rows", () => {
  const placed = () => {
    const machine = machineFor(place("5.0"));
    const fx = effects();
    machine.step(inputs(), fx);
    fx.place.mockClear();
    return { machine, fx };
  };

  it("moves to where its locator went", () => {
    const { machine, fx } = placed();
    const lost = place("5.0", -30, { identity: { row: { itemId: "5.0", rank: 0 }, locator: "loc-5" } });
    machine.lost(lost, inputs({ items: [row("s1.cp.5.0", { reading_locator: "loc-5" })] }), fx);
    expect(fx.place).toHaveBeenCalledWith(at("s1.cp.5.0", -30, 1));
    expect(machine.snapshot().visible).toBe(true);
  });

  it("does nothing when the same row is still loaded", () => {
    const { machine, fx } = placed();
    machine.lost(place("5.0"), inputs(), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(fx.loadOlder).not.toHaveBeenCalled();
  });

  it("is searched for again, hidden, and never drops the reload bookmark", () => {
    const { machine, fx } = placed();
    machine.lost(place("5.0"), inputs({ items: rows(10, 16), hasMoreOlder: true }), fx);
    expect(fx.loadOlder).toHaveBeenCalledTimes(1);
    expect(machine.snapshot()).toMatchObject({ visible: false, searching: true });
    machine.step(inputs({ items: rows(10, 16), hasMoreOlder: true, loadingOlder: true }), fx);
    machine.step(inputs({ items: rows(10, 16), hasMoreOlder: true }), fx);
    expect(fx.place).toHaveBeenCalledWith(TAIL);
    expect(fx.dropSessionPlace).not.toHaveBeenCalled();
  });

  it("is ignored before the opening place is reached", () => {
    const machine = machineFor(place("40.0"));
    const fx = effects();
    machine.lost(place("40.0"), inputs({ loading: true }), fx);
    expect(fx.place).not.toHaveBeenCalled();
    expect(fx.loadOlder).not.toHaveBeenCalled();
  });
});

describe("snapshot", () => {
  it("starts hidden, publishes only changes, and becomes visible once placed", () => {
    const machine = machineFor(null);
    const listener = vi.fn();
    machine.subscribe(listener);
    expect(machine.snapshot()).toEqual({ visible: false, searching: false, pin: null, fillAllowed: true });
    machine.step(inputs({ items: [], loading: true }), effects());
    expect(listener).not.toHaveBeenCalled();
    machine.step(inputs(), effects());
    expect(listener).toHaveBeenCalledTimes(1);
    expect(machine.snapshot()).toEqual({ visible: true, searching: false, pin: null, fillAllowed: true });
    expect(machine.isPlaced()).toBe(true);
  });

  it("pins the placed row, then the reader's own row, and nothing at the tail", () => {
    const machine = machineFor(place("4.0"));
    machine.step(inputs(), effects());
    expect(machine.snapshot().pin).toEqual({ itemId: "4.0", rank: 0 });
    machine.readerMoved(inputs(), { itemId: "2.0", rank: 0 });
    expect(machine.snapshot().pin).toEqual({ itemId: "2.0", rank: 0 });
    machine.readerMoved(inputs(), null);
    expect(machine.snapshot().pin).toBeNull();
  });
});
