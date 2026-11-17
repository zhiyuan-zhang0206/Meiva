import type { BackendTimelineItem, ReadingWindowResponse, TimelineResponse } from "@/lib/contracts/types";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { parseItemIdParts } from "../timeline";
import { readingBlock } from "./identity";

export interface ReadingCandidate {
  key: string;
  item: BackendTimelineItem;
  position: SavedScroll;
}

export class ReadingCoordinatesChangedError extends Error {
  constructor() {
    super("The source changed its reading coordinates; retry this position");
    this.name = "ReadingCoordinatesChangedError";
  }
}

export function confirmedAnchor(page: ReadingWindowResponse): BackendTimelineItem {
  const matches = page.items.filter((item) => item.item_id === page.anchor_item_id);
  if (!page.reading_source || matches.length !== 1 || !matches[0].reading_locator) {
    throw new Error("The reading source did not confirm its canonical anchor");
  }
  return matches[0];
}

export function canonicalPosition(position: SavedScroll, item: BackendTimelineItem): SavedScroll {
  if (!position.anchor || !item.reading_locator) throw new Error("A source-qualified reading anchor is required");
  return { ...position, followBottom: false, anchor: {
    ...position.anchor, itemId: item.item_id, rank: parseItemIdParts(item.item_id)?.rank ?? 0,
    readingLocator: item.reading_locator,
  } };
}

export function mergeReadingItems(pages: readonly ReadingWindowResponse[]): BackendTimelineItem[] {
  const items: BackendTimelineItem[] = [];
  const seen = new Map<string, number>();
  const coordinates = new Map<string, string>();
  for (const page of pages) for (const item of page.items) {
    if (!page.reading_source) throw new Error("The reading source did not confirm its window");
    const key = item.reading_locator ?? JSON.stringify([page.reading_source, item.item_id]);
    const index = seen.get(key);
    const previousKey = coordinates.get(item.item_id);
    if ((previousKey !== undefined && previousKey !== key) ||
      (index !== undefined && (items[index].item_id !== item.item_id ||
        (items[index].source_message_id ?? null) !== (item.source_message_id ?? null) ||
        (items[index].source_inbound_id ?? null) !== (item.source_inbound_id ?? null) ||
        readingBlock(items[index]) !== readingBlock(item)))) throw new ReadingCoordinatesChangedError();
    coordinates.set(item.item_id, key);
    if (index === undefined) { seen.set(key, items.length); items.push(item); }
    else items[index] = item;
  }
  return items;
}

export function overlapsAcceptedHead(items: readonly BackendTimelineItem[], head: TimelineResponse): boolean {
  return items.some((row) => row.reading_locator && head.items.some((item) =>
    item.reading_locator === row.reading_locator && readingBlock(row) === readingBlock(item) &&
    (item.source_message_id ?? null) === (row.source_message_id ?? null) &&
    (item.source_inbound_id ?? null) === (row.source_inbound_id ?? null)));
}
