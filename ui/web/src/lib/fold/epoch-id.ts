export interface EpochItemId {
  epoch: number;
  local: string;
}

export const EPOCH_SWITCH_LOCAL = "sw";

const EPOCH_ID = /^e(\d+)\.(.+)$/;

export function parseEpochItemId(itemId: string): EpochItemId | null {
  const m = EPOCH_ID.exec(itemId);
  if (m === null) return null;
  const epoch = Number(m[1]);
  return Number.isSafeInteger(epoch) ? { epoch, local: m[2] } : null;
}

export function isEpochItemId(itemId: string): boolean {
  return parseEpochItemId(itemId) !== null;
}

export function isSyntheticEpochItemId(itemId: string): boolean {
  const parsed = parseEpochItemId(itemId);
  return parsed !== null && parsed.local === EPOCH_SWITCH_LOCAL;
}
