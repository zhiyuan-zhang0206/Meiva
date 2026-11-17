import { describe, expect, it } from "vitest";
import type { BackendTimelineItem, ReadingWindowResponse } from "@/lib/contracts/types";
import { candidateKey, sameReadingIdentity } from "./identity";
import { canonicalPosition, confirmedAnchor, mergeReadingItems, overlapsAcceptedHead, ReadingCoordinatesChangedError } from "./plan";

function item(overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return { item_id: "2.0", kind: "agent_chat", payload: "saved", source: null, created_at: null,
    inbound_id: null, show_timestamp: true, source_message_id: "stable", source_block_idx: 0, ...overrides };
}

function page(items: BackendTimelineItem[]): ReadingWindowResponse {
  return { items, reading_source: "domain", anchor_item_id: items[0]?.item_id, older_cursor: null, newer_cursor: null };
}

describe("source-qualified reading decisions", () => {
  it("matches native stable identity after local index changes and rejects index-only matches", () => {
    expect(sameReadingIdentity(item(), item({ item_id: "s2.cp.9.0" }))).toBe(true);
    expect(sameReadingIdentity(item(), item({ source_message_id: "different" }))).toBe(false);
    expect(sameReadingIdentity(item(), item({ source_block_idx: 1 }))).toBe(false);
    expect(sameReadingIdentity(item({ source_inbound_id: 9 }), item({ source_inbound_id: 10 }))).toBe(false);
    expect(sameReadingIdentity(item({ source_message_id: null }), item())).toBe(false);
  });

  it("requires both record coordinates and any supplied stable identities", () => {
    expect(sameReadingIdentity(item({ item_id: "h42.0" }), item({ item_id: "h43.0" }))).toBe(false);
    expect(sameReadingIdentity(item({ item_id: "h42.0" }), item({ item_id: "h42.0" }))).toBe(true);
    expect(sameReadingIdentity(item({ item_id: "h42.0" }), item({ item_id: "s1.cp.h42.0" }))).toBe(true);
  });

  it("waits for complete items and uses a real streamed block with its stable message id", () => {
    expect(candidateKey(item({ partial: true }))).toBeNull();
    expect(candidateKey(item({ source_message_id: null }))).toBeNull();
    expect(candidateKey(item({ item_id: "8.2", source_block_idx: null }))).toBe(
      candidateKey(item({ item_id: "13.2", source_block_idx: 2 })));
  });

  it("requires the actual canonical member and preserves its measured viewport offset", () => {
    const canonical = item({ item_id: "s1.cp.0.0", reading_locator: "qualified" });
    expect(confirmedAnchor(page([canonical]))).toBe(canonical);
    const position = canonicalPosition({ contentKey: "7", scrollTop: 900, followBottom: false,
      anchor: { itemId: "2.0", rank: 0, viewportTop: -17 } }, canonical);
    expect(position.anchor).toEqual({ itemId: "s1.cp.0.0", rank: 1, viewportTop: -17, readingLocator: "qualified" });
    expect(() => confirmedAnchor({ ...page([canonical]), anchor_item_id: "other" })).toThrow();
    expect(() => confirmedAnchor(page([canonical, canonical]))).toThrow();
  });

  it("merges source-qualified rows without losing identityless interior context", () => {
    const old = item({ item_id: "s2.cp.1.0", reading_locator: "one" });
    const updated = { ...old, payload: "updated source payload" };
    const context = item({ item_id: "s2.cp.2.0", source_message_id: null });
    const newer = item({ item_id: "3.0", reading_locator: "two" });
    expect(mergeReadingItems([page([old, context]), page([updated, newer])])).toEqual([updated, context, newer]);
  });

  it("requires a fresh anchor when a retained source changes its canonical display prefix", () => {
    const old = item({ item_id: "s2.cp.1.0", reading_locator: "one" });
    const moved = { ...old, item_id: "s3.cp.1.0" };
    expect(() => mergeReadingItems([page([old]), page([moved])])).toThrow(ReadingCoordinatesChangedError);
    expect(old.item_id).toBe("s2.cp.1.0");
  });

  it.each([
    { reading_locator: "another_domain", source_message_id: "different" },
    { reading_locator: "one", source_message_id: "different" },
    { reading_locator: "one", source_block_idx: 1 },
  ])("refuses ambiguous DOM/expansion coordinates despite locator metadata %j", (changed) => {
    const old = item({ reading_locator: "one" });
    const replacement = item(changed);
    expect(() => mergeReadingItems([page([old]), page([replacement])])).toThrow(ReadingCoordinatesChangedError);
  });

  it("joins only matching source-qualified visible members in a fresh accepted head", () => {
    const visible = item({ reading_locator: "source_one" });
    const head = { items: [visible], msg_count: 3, has_more: false };
    expect(overlapsAcceptedHead([visible], head)).toBe(true);
    expect(overlapsAcceptedHead([visible], { ...head, items: [item({ reading_locator: "other_domain" })] })).toBe(false);
    expect(overlapsAcceptedHead([visible], { ...head, items: [item({ reading_locator: "source_one", source_message_id: "different" })] })).toBe(false);
    expect(overlapsAcceptedHead([visible], { ...head, items: [] })).toBe(false);
  });
});
