export function isLiveOverlayId(itemId: string): boolean {
  return itemId.startsWith("live.");
}

export function isDisplayOnlyItemId(itemId: string): boolean {
  return itemId.startsWith("_") || isLiveOverlayId(itemId);
}
