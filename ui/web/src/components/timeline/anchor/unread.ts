import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { standingHeadNoteIds } from "@/lib/timeline/timeline";

import { isSilentTimelineItem } from "../model/runs";
import type { RowRef } from "./dom";
import { identify, rankOf } from "./rows";
import type { TailDeparture } from "./saved";

const isHumanMessage = (item: BackendTimelineItem) => item.kind === "inbound_chat" && item.source === "user";
const isReply = (item: BackendTimelineItem) => item.kind === "agent_chat" && item.user_facing !== false && item.payload.length > 0;
const refOf = (item: BackendTimelineItem, buffer: CompactTransitionBuffer | null): RowRef => ({
  itemId: item.item_id,
  rank: rankOf(item, buffer),
});

export function tailDeparture(items: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): TailDeparture {
  const last = items.findLast((item) => !item.item_id.startsWith("_") && !isSilentTimelineItem(item) && (isHumanMessage(item) || isReply(item)));
  return last ? {
    last: identify(refOf(last, buffer), items, buffer),
    ...(last.partial ? { partialPayload: last.payload } : {}),
  } : { last: null };
}

function departureIndex(
  items: readonly BackendTimelineItem[], departure: TailDeparture, buffer: CompactTransitionBuffer | null,
): number {
  const { last, partialPayload } = departure;
  return last === null ? -1 : items.findIndex((item) => last.locator
    ? item.reading_locator === last.locator
    : item.item_id === last.row.itemId && rankOf(item, buffer) === last.row.rank &&
      item.kind === last.content?.kind && (partialPayload !== undefined || item.payload === last.content.payload));
}

export function unreadBoundary(
  items: readonly BackendTimelineItem[], itemId: string | null, departure: TailDeparture | undefined,
  buffer: CompactTransitionBuffer | null,
): { row: RowRef | null; needsOlder: boolean } {
  const delivery = itemId === null ? -1 : items.findIndex((item) => item.item_id === itemId);
  const watermark = departure ? departureIndex(items, departure, buffer) : -1;
  const missingDeparture = departure?.last != null && watermark === -1;
  const stream = departure ? items.findIndex((item, index) => item.userDelivery === undefined && isReply(item) &&
    (index > watermark || (index === watermark && departure.partialPayload !== undefined && item.payload !== departure.partialPayload))) : -1;
  const index = delivery === -1 ? stream : stream === -1 ? delivery :
    missingDeparture ? delivery : Math.min(stream, delivery);
  if (index < 0) return { row: null, needsOlder: missingDeparture };
  const reply = items[index];
  const row = refOf(reply, buffer);
  if (reply.kind !== "agent_chat" || reply.nonHumanWake) return { row, needsOlder: missingDeparture };
  const headNotes = standingHeadNoteIds(items);
  for (let before = index - 1; before >= 0; before--) {
    const item = items[before];
    if (rankOf(item, buffer) === row.rank) {
      if (isHumanMessage(item) && !isSilentTimelineItem(item)) return { row: refOf(item, buffer), needsOlder: missingDeparture };
      if (item.kind === "system_prompt" || headNotes.has(item.item_id)) continue;
      if (item.kind === "agent_reasoning" || item.kind === "agent_code" || item.kind === "code_output" ||
          item.kind === "attach" || (item.kind === "system_marker" && item.source === "reply_visibility")) continue;
    }
    return { row, needsOlder: missingDeparture };
  }
  return { row, needsOlder: true };
}
