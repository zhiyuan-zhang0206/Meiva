import type { BackendTimelineItem } from "@/lib/contracts/types";
import { parseItemIdParts } from "../timeline";

export function readingBlock(item: BackendTimelineItem): number | null {
  return item.source_block_idx ?? parseItemIdParts(item.item_id)?.block ?? null;
}

export function candidateKey(item: BackendTimelineItem): string | null {
  const block = readingBlock(item);
  if (item.kind === "system_prompt" || item.partial || block === null) return null;
  if (item.reading_locator) return item.reading_locator;
  if (!item.source_message_id && !item.source_inbound_id) return null;
  return JSON.stringify([item.item_id.startsWith("h") ? item.item_id : null,
    item.source_message_id ?? null, item.source_inbound_id ?? null, block]);
}

export function sameReadingIdentity(a: BackendTimelineItem, b: BackendTimelineItem): boolean {
  if (!a.source_message_id && !a.source_inbound_id) return false;
  return readingBlock(a) !== null && readingBlock(a) === readingBlock(b) &&
    (!a.source_message_id || a.source_message_id === b.source_message_id) &&
    (!a.source_inbound_id || a.source_inbound_id === b.source_inbound_id) &&
    (!a.item_id.startsWith("h") || !b.item_id.startsWith("h") || a.item_id === b.item_id);
}
