import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";

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

function unpublishedReply(
  items: readonly BackendTimelineItem[], departure: TailDeparture, buffer: CompactTransitionBuffer | null,
): number {
  const { last, partialPayload } = departure;
  const watermark = last === null ? -1 : items.findIndex((item) => last.locator
    ? item.reading_locator === last.locator
    : item.item_id === last.row.itemId && rankOf(item, buffer) === last.row.rank &&
      item.kind === last.content?.kind && (partialPayload !== undefined || item.payload === last.content.payload));
  if (watermark === -1 && last !== null) return -1;
  // meiva:note
  // A stream can appear (or grow in place) before it has a delivery cursor. Once it has
  // a delivery, the entry's server seen cursor is authoritative for that row.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  return items.findIndex((item, index) => item.userDelivery === undefined && isReply(item) &&
    (index > watermark || (index === watermark && partialPayload !== undefined && item.payload !== partialPayload)));
}

export function unreadBoundary(
  items: readonly BackendTimelineItem[], itemId: string | null, departure: TailDeparture | undefined,
  buffer: CompactTransitionBuffer | null,
): RowRef | null {
  const delivery = itemId === null ? -1 : items.findIndex((item) => item.item_id === itemId);
  const stream = departure ? unpublishedReply(items, departure, buffer) : -1;
  const index = delivery === -1 ? stream : stream === -1 ? delivery : Math.min(stream, delivery);
  if (index < 0) return null;
  const reply = items[index];
  const boundary = refOf(reply, buffer);
  if (reply.kind !== "agent_chat" || reply.nonHumanWake) return boundary;
  for (let before = index - 1; before >= 0; before--) {
    const item = items[before];
    if (rankOf(item, buffer) !== boundary.rank) break;
    if (isHumanMessage(item) && !isSilentTimelineItem(item)) return refOf(item, buffer);
    if (item.kind === "agent_reasoning" || item.kind === "agent_code" || item.kind === "code_output" ||
        item.kind === "attach" || (item.kind === "system_marker" && item.source === "reply_visibility")) continue;
    break;
  }
  return boundary;
}
