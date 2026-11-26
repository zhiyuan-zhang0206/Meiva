import type { BackendTimelineItem } from "../contracts/types";

export function isLiveOverlayId(itemId: string): boolean {
  return itemId.startsWith("live.");
}

export function isDisplayOnlyItemId(itemId: string): boolean {
  return itemId.startsWith("_") || isLiveOverlayId(itemId);
}

// meiva:link ui/web/src/docs/frontend-state/timeline-cache.md
export function dropLiveOverlays(items: BackendTimelineItem[]): BackendTimelineItem[] {
  if (!items.some((it) => isLiveOverlayId(it.item_id))) return items;
  return items.filter((it) => !isLiveOverlayId(it.item_id));
}
