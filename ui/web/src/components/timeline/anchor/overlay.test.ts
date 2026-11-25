import { describe, expect, it } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { fakeBrowser } from "@/lib/browser/testing";
import { findOldestTimelineCursor } from "@/lib/timeline/timeline";

import { readReloadBookmark, saveReloadBookmark } from "../model/reading-position";
import { tailDeparture, unreadBoundary } from "./unread";

const LIVE = "live.t1.msg_a:0";

function row(itemId: string, overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return {
    item_id: itemId,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

describe("an overlay row is not a reading or paging position", () => {
  const user = row("3.0", { kind: "inbound_chat", source: "user", payload: "q" });
  const reply = row("h120.0", { payload: "done", created_at: "2026-01-01T00:00:01Z" });
  const live = row(LIVE, { payload: "writing", partial: true });

  it("is never the last thing the reader saw, nor the unread boundary", () => {
    expect(tailDeparture([user, reply, live], null).last?.row.itemId).toBe("h120.0");
    expect(tailDeparture([user, live], null).last?.row.itemId).toBe("3.0");
    const departure = tailDeparture([user], null);
    expect(unreadBoundary([user, live], { itemId: null }, departure, null).row).toBeNull();
  });

  it("is not stored as a reload bookmark", () => {
    const browser = fakeBrowser({ session: {} });
    const place = { itemId: LIVE, rank: 0, viewportTop: 10, scrollTop: 100, savedAt: 1 };
    saveReloadBookmark(browser.session, "42", place, 1);
    expect(readReloadBookmark(browser.session, "42", 2)).toBeNull();
    saveReloadBookmark(browser.session, "42", { ...place, itemId: "h120.0" }, 1);
    expect(readReloadBookmark(browser.session, "42", 2)?.itemId).toBe("h120.0");
  });

  it("is never the oldest paging cursor, even as the only loaded row", () => {
    expect(findOldestTimelineCursor([live])).toBeUndefined();
    expect(findOldestTimelineCursor([live, reply])?.item_id).toBe("h120.0");
  });
});
