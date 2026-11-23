import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline/timeline";

import { isSilentTimelineItem } from "../model/runs";
import type { RowRef } from "./dom";
import { identify, rankOf } from "./rows";
import type { TailDeparture } from "./saved";

const isHumanMessage = (item: BackendTimelineItem) => item.kind === "inbound_chat" && item.source === "user";
const isReply = (item: BackendTimelineItem) => item.kind === "agent_chat" && item.user_facing !== false && item.payload.length > 0;
const sourceBlock = (item: BackendTimelineItem) => item.source_block_idx ?? parseItemIdParts(item.item_id)?.block;
const refOf = (item: BackendTimelineItem, buffer: CompactTransitionBuffer | null): RowRef => ({
  itemId: item.item_id,
  rank: rankOf(item, buffer),
});

export function tailDeparture(items: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): TailDeparture {
  const last = items.findLast((item) => !item.item_id.startsWith("_") && !isSilentTimelineItem(item) && (isHumanMessage(item) || isReply(item)));
  return last ? {
    last: identify(refOf(last, buffer), items, buffer),
    ...(last.partial ? { partialPayload: last.payload } : {}),
    ...(last.source_message_id ? { sourceMessage: { id: last.source_message_id, block: sourceBlock(last) } } : {}),
  } : { last: null };
}

interface NativeLocator {
  version: 1;
  source: "native";
  agent_id: number;
  epoch: string;
  checkpoint_id?: string;
  message_id?: string;
  inbound_id?: number;
  block: number;
}

function nativeLocator(value: string): NativeLocator | null {
  try {
    const bytes = Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), (byte) => byte.charCodeAt(0));
    const locator = JSON.parse(new TextDecoder().decode(bytes)) as Partial<Record<keyof NativeLocator, unknown>> | null;
    return locator?.version === 1 && locator.source === "native" && typeof locator.agent_id === "number" &&
      typeof locator.epoch === "string" && typeof locator.block === "number" &&
      (typeof locator.message_id === "string" || typeof locator.inbound_id === "number") ? locator as NativeLocator : null;
  } catch {
    return null;
  }
}

// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
function archivedDepartureIndex(items: readonly BackendTimelineItem[], locator: string): number {
  const before = nativeLocator(locator);
  if (!before || before.checkpoint_id != null) return -1;
  return items.findIndex((item) => {
    const after = item.reading_locator ? nativeLocator(item.reading_locator) : null;
    return after?.checkpoint_id != null && after.agent_id === before.agent_id &&
      after.epoch === before.epoch && after.block === before.block &&
      after.message_id === before.message_id && after.inbound_id === before.inbound_id;
  });
}

function departureIndex(
  items: readonly BackendTimelineItem[], departure: TailDeparture, buffer: CompactTransitionBuffer | null,
): number {
  const { last, partialPayload, sourceMessage } = departure;
  if (last === null) return -1;
  if (last.locator) {
    const exact = items.findIndex((item) => item.reading_locator === last.locator);
    return exact >= 0 ? exact : archivedDepartureIndex(items, last.locator);
  }
  if (sourceMessage) {
    if (sourceMessage.block === undefined) return -1;
    return items.findIndex((item) => item.source_message_id === sourceMessage.id &&
      item.kind === last.content?.kind && sourceBlock(item) === sourceMessage.block);
  }
  return items.findIndex((item) => item.item_id === last.row.itemId && rankOf(item, buffer) === last.row.rank &&
    item.kind === last.content?.kind && (partialPayload !== undefined || item.payload === last.content.payload));
}

export function unreadBoundary(
  items: readonly BackendTimelineItem[], reveal: { itemId: string | null; entrySeenId?: number },
  departure: TailDeparture | undefined,
  buffer: CompactTransitionBuffer | null,
): { row: RowRef | null; needsOlder: boolean } {
  const { itemId, entrySeenId } = reveal;
  const delivery = itemId === null ? -1 : items.findIndex((item) => item.item_id === itemId);
  const watermark = departure ? departureIndex(items, departure, buffer) : -1;
  const missingDeparture = departure?.last != null && watermark === -1;
  let index = delivery;
  if (departure && !(missingDeparture && delivery >= 0)) {
    const partialPrefix = departure.partialPayload?.trimEnd();
    index = items.findIndex((item, position) => {
      const grewAtWatermark = position === watermark && partialPrefix !== undefined && partialPrefix.length > 0 &&
        item.payload.startsWith(partialPrefix) && item.payload.trimEnd().length > partialPrefix.length;
      if (position <= watermark && !grewAtWatermark) return false;
      if (position === delivery) return true;
      const publication = item.userDelivery;
      return isReply(item) && (publication === undefined ||
        (watermark >= 0 && entrySeenId !== undefined && publication.content !== null && publication.id > entrySeenId));
    });
  }
  if (index < 0) return { row: null, needsOlder: missingDeparture };
  const reply = items[index];
  const row = refOf(reply, buffer);
  if (reply.kind !== "agent_chat" || reply.nonHumanWake) return { row, needsOlder: missingDeparture };
  const headNotes = standingHeadNoteIds(items);
  for (let before = index - 1; before >= 0; before--) {
    const item = items[before];
    if (rankOf(item, buffer) === row.rank) {
      if (isHumanMessage(item) && !isSilentTimelineItem(item)) return { row: refOf(item, buffer), needsOlder: missingDeparture };
      if (item.kind === "inbound_compact_summary" && row.rank > 0) return { row, needsOlder: missingDeparture };
      if (isReattachedTimelineContext(item) || headNotes.has(item.item_id)) continue;
      if (item.kind === "agent_reasoning" || item.kind === "agent_code" || item.kind === "code_output" ||
          item.kind === "attach" || (item.kind === "system_marker" && item.source === "reply_visibility")) continue;
    }
    return { row, needsOlder: missingDeparture };
  }
  return { row, needsOlder: true };
}
