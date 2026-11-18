import { describe, expect, it } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";
import {
  readReadingBookmark, saveReadingBookmark, READING_BOOKMARKS_KEY,
  READING_BOOKMARK_MAX, READING_BOOKMARK_TTL_MS, type ReadingBookmark,
} from "./bookmarks";

function saved(agentId: number, savedAt = 1000): ReadingBookmark {
  return { agentId, savedAt, locator: `source_${agentId}`, position: {
    contentKey: String(agentId), scrollTop: 600, followBottom: false,
    anchor: { itemId: "1.0", rank: 0, viewportTop: 97 }, keepSendReserve: true,
  } };
}

describe("per-tab reading metadata", () => {
  it("round trips each agent's source and offset without bodies or extra fields", () => {
    const browser = fakeBrowser({ session: {}, local: {} });
    expect(readReadingBookmark(browser.session, 1, 1000)).toEqual({ state: "ready", bookmark: null });
    const candidate = { ...saved(1), chat: "private body", position: { ...saved(1).position, body: "private body" } };
    expect(saveReadingBookmark(browser.session, candidate, 1000)).toBe("stored");
    expect(saveReadingBookmark(browser.session, saved(2), 1000)).toBe("stored");
    expect(readReadingBookmark(browser.session, 1, 1001)).toEqual({ state: "ready", bookmark: saved(1) });
    expect(readReadingBookmark(browser.session, 2, 1001)).toEqual({ state: "ready", bookmark: saved(2) });
    expect(browser.sessionValues.get(READING_BOOKMARKS_KEY)).not.toContain("private body");
    expect(browser.localValues.has(READING_BOOKMARKS_KEY)).toBe(false);
  });

  it("caps the map at 64 agents and expires entries without writing on a read", () => {
    const browser = fakeBrowser({ session: {} });
    for (let id = 1; id <= READING_BOOKMARK_MAX + 1; id++) {
      expect(saveReadingBookmark(browser.session, saved(id, 1000 + id), 1000 + id)).toBe("stored");
    }
    expect(readReadingBookmark(browser.session, 1, 2000)).toEqual({ state: "ready", bookmark: null });
    expect(readReadingBookmark(browser.session, 65, 2000)).toEqual({ state: "ready", bookmark: saved(65, 1065) });
    const previous = browser.sessionValues.get(READING_BOOKMARKS_KEY);
    expect(readReadingBookmark(browser.session, 65, 1065 + READING_BOOKMARK_TTL_MS)).toEqual({ state: "ready", bookmark: null });
    expect(browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(previous);
  });

  it.each(["not-json", '{"version":2,"entries":[]}', '{"version":1,"entries":[{}]}'])(
    "preserves unrecognized storage bytes: %s", (raw) => {
      const browser = fakeBrowser({ session: { [READING_BOOKMARKS_KEY]: raw } });
      expect(readReadingBookmark(browser.session, 1, 1000)).toEqual({ state: "invalid" });
      expect(saveReadingBookmark(browser.session, saved(1), 1000)).toBe("invalid");
      expect(browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(raw);
    },
  );

  it("keeps the old record on unavailable reads or rejected writes", () => {
    const browser = fakeBrowser({ session: {} });
    expect(saveReadingBookmark(browser.session, saved(1), 1000)).toBe("stored");
    const original = browser.sessionValues.get(READING_BOOKMARKS_KEY);
    browser.refuseStorage(true);
    expect(readReadingBookmark(browser.session, 1, 1001)).toEqual({ state: "unavailable" });
    expect(saveReadingBookmark(browser.session, saved(1, 1001), 1001)).toBe("unavailable");
    browser.refuseStorage(false);
    browser.failStorage("session", "write");
    expect(saveReadingBookmark(browser.session, saved(1, 1001), 1001)).toBe("unavailable");
    expect(browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
  });

  it("rejects mismatched agent geometry and invalid coordinates without a write", () => {
    const browser = fakeBrowser({ session: {} });
    const candidate = saved(1);
    candidate.position.contentKey = "2";
    expect(saveReadingBookmark(browser.session, candidate, 1000)).toBe("invalid");
    candidate.position.contentKey = "1";
    candidate.position.anchor.viewportTop = Number.NaN;
    expect(saveReadingBookmark(browser.session, candidate, 1000)).toBe("invalid");
    expect(browser.sessionValues.has(READING_BOOKMARKS_KEY)).toBe(false);
  });

  it("preserves future timestamps after a clock rollback", () => {
    const raw = JSON.stringify({ version: 1, entries: [saved(1, 2000)] });
    const browser = fakeBrowser({ session: { [READING_BOOKMARKS_KEY]: raw } });
    expect(readReadingBookmark(browser.session, 1, 1000)).toEqual({ state: "invalid" });
    expect(saveReadingBookmark(browser.session, saved(2), 1000)).toBe("invalid");
    expect(browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(raw);
  });
});
