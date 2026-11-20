import { describe, expect, it } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";

import { readReloadBookmark, RELOAD_BOOKMARKS_STORAGE_KEY, type ReloadRestoreConfig } from "../model/reading-position";
import { hasSavedPlace, readMemoryPlace, readSessionPlace, rowAnchor, writeMemoryPlace, writeSessionPlace, type SavedPlace } from "./saved";

const place = (extra: Partial<SavedPlace> = {}): SavedPlace => ({
  identity: { row: { itemId: "7.0", rank: 0 } },
  offset: -120,
  sendReserve: false,
  holdReserve: false,
  ...extra,
});

function enabled(now = 1000) {
  const browser = fakeBrowser({ session: {} });
  const config: ReloadRestoreConfig = { state: "enabled", session: browser.session, now: () => now };
  return { browser, config };
}

describe("memory place", () => {
  it("returns per entry and content", () => {
    const saved = place({ sendReserve: true });
    writeMemoryPlace("entry-a", "42", saved);
    expect(readMemoryPlace("entry-a", "42")).toBe(saved);
    expect(readMemoryPlace("entry-a", "43")).toBeNull();
    expect(readMemoryPlace("entry-b", "42")).toBeNull();
  });

  it("is keyed by entry: a later save for other content replaces it", () => {
    writeMemoryPlace("entry-switch", "42", place());
    writeMemoryPlace("entry-switch", "43", place({ offset: 0 }));
    expect(readMemoryPlace("entry-switch", "42")).toBeNull();
    expect(readMemoryPlace("entry-switch", "43")?.offset).toBe(0);
  });

  it("needs both an entry and a content key", () => {
    writeMemoryPlace(undefined, "42", place());
    writeMemoryPlace("entry-no-content", undefined, place());
    expect(readMemoryPlace(undefined, "42")).toBeNull();
    expect(readMemoryPlace("entry-no-content", undefined)).toBeNull();
  });
});

describe("session place", () => {
  it("round-trips the row, its locator, the turn flag, the offset and both reserves", () => {
    const { config } = enabled();
    const saved = place({
      identity: { row: { itemId: "s1.cp.4.0", rank: 1, turn: true }, locator: "loc-4", coordinate: { rank: 1, msg: 4, block: 0 } },
      offset: 56,
      sendReserve: true,
      holdReserve: true,
    });
    writeSessionPlace("42", config, saved, 1234);
    // meiva:note
    // The coordinate only serves a compact inside one page: a reload re-reads the rows.
    expect(readSessionPlace("42", config)).toEqual({
      identity: { row: { itemId: "s1.cp.4.0", rank: 1, turn: true }, locator: "loc-4" },
      offset: 56,
      sendReserve: true,
      holdReserve: true,
    });
  });

  it("round-trips a plain item row without optional fields", () => {
    const { config, browser } = enabled();
    writeSessionPlace("42", config, place(), -5);
    expect(readSessionPlace("42", config)).toEqual(place());
    expect(readReloadBookmark(browser.session, "42", 1000)).toEqual({ itemId: "7.0", rank: 0, viewportTop: -120, scrollTop: 0, savedAt: 1000 });
  });

  it("clears on the tail, so the next reload opens on the latest rows", () => {
    const { config, browser } = enabled();
    writeSessionPlace("42", config, place(), 900);
    writeSessionPlace("43", config, place(), 900);
    writeSessionPlace("42", config, null, 0);
    expect(readSessionPlace("42", config)).toBeNull();
    expect(readSessionPlace("43", config)).not.toBeNull();
    writeSessionPlace("43", config, null, 0);
    expect(browser.sessionValues.has(RELOAD_BOOKMARKS_STORAGE_KEY)).toBe(false);
  });

  it("keeps nothing while reload restore is off or its settings are loading", () => {
    const { config, browser } = enabled();
    writeSessionPlace("42", config, place(), 900);
    expect(readSessionPlace("42", { state: "disabled" })).toBeNull();
    expect(readSessionPlace("42", { state: "loading" })).toBeNull();
    writeSessionPlace("42", { state: "disabled" }, null, 0);
    writeSessionPlace("44", { state: "loading" }, place(), 0);
    expect(readSessionPlace("42", config)).not.toBeNull();
    expect(readReloadBookmark(browser.session, "44", 1000)).toBeNull();
    expect(readSessionPlace(undefined, config)).toBeNull();
  });
});

describe("hasSavedPlace", () => {
  it("is true for a memory place or a reload bookmark", () => {
    const { config } = enabled();
    expect(hasSavedPlace("entry-has", "42", config)).toBe(false);
    writeSessionPlace("42", config, place(), 0);
    expect(hasSavedPlace("entry-has", "42", config)).toBe(true);
    expect(hasSavedPlace("entry-has", "42", { state: "disabled" })).toBe(false);
    writeMemoryPlace("entry-has", "42", place());
    expect(hasSavedPlace("entry-has", "42", { state: "disabled" })).toBe(true);
  });
});

describe("rowAnchor", () => {
  it("places the saved row, or the row it was found as, at the saved offset", () => {
    const saved = place();
    expect(rowAnchor(saved)).toEqual({ kind: "row", row: { itemId: "7.0", rank: 0 }, offset: -120 });
    expect(rowAnchor(saved, { itemId: "s1.cp.7.0", rank: 1 })).toEqual({ kind: "row", row: { itemId: "s1.cp.7.0", rank: 1 }, offset: -120 });
  });
});
