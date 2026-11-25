import type { BackendTimelineItem } from "../contracts/types";

import { EPOCH_SWITCH_LOCAL, EPOCH_UNAVAILABLE_LOCAL, parseEpochItemId } from "./epoch-id";

export function parseItemId(itemId: string): [number, number] | null {
  const parts = itemId.split(".");
  if (parts.length !== 2) return null;
  const rawMsg = parts[0].startsWith("h") ? parts[0].slice(1) : parts[0];
  const msgIdx = Number(rawMsg);
  const blockIdx = Number(parts[1]);
  if (!Number.isFinite(msgIdx) || !Number.isFinite(blockIdx)) return null;
  return [msgIdx, blockIdx];
}

export interface ItemIdParts {
  rank: number;
  checkpointId: string | null;
  msg: number;
  block: number;
}

export function parseItemIdParts(itemId: string): ItemIdParts | null {
  const current = parseItemId(itemId);
  if (current !== null) {
    const [msg, block] = current;
    if (!Number.isInteger(msg) || !Number.isInteger(block) || msg < 0 || block < 0) {
      return null;
    }
    return { rank: 0, checkpointId: null, msg, block };
  }

  const parts = itemId.split(".");
  if (parts.length !== 4) return null;
  const [segment, checkpointId, msgPart, blockPart] = parts;
  if (!/^s[1-9]\d*$/.test(segment) || checkpointId.length === 0) return null;
  const rank = Number(segment.slice(1));
  const rawMsg = msgPart.startsWith("h") ? msgPart.slice(1) : msgPart;
  const msg = Number(rawMsg);
  const block = Number(blockPart);
  if (
    !Number.isSafeInteger(rank) ||
    !Number.isSafeInteger(msg) ||
    !Number.isSafeInteger(block) ||
    msg < 0 ||
    block < 0
  ) {
    return null;
  }
  return { rank, checkpointId, msg, block };
}

interface OrderKey {
  epoch: number;
  slot: number;
  rank: number;
  msg: number;
  block: number;
}

const CURRENT_EPOCH = Number.POSITIVE_INFINITY;
const SLOT_SWITCH = 0;
const SLOT_UNAVAILABLE = 1;
const SLOT_CONTENT = 2;

function orderKey(itemId: string): OrderKey | null {
  const prefixed = parseEpochItemId(itemId);
  const epoch = prefixed?.epoch ?? CURRENT_EPOCH;
  const local = prefixed?.local ?? itemId;
  if (prefixed !== null && local === EPOCH_SWITCH_LOCAL) {
    return { epoch, slot: SLOT_SWITCH, rank: 0, msg: 0, block: 0 };
  }
  if (prefixed !== null && local === EPOCH_UNAVAILABLE_LOCAL) {
    return { epoch, slot: SLOT_UNAVAILABLE, rank: 0, msg: 0, block: 0 };
  }
  const parts = parseItemIdParts(local);
  if (parts === null) return null;
  return { epoch, slot: SLOT_CONTENT, rank: parts.rank, msg: parts.msg, block: parts.block };
}

function compareKeys(a: OrderKey, b: OrderKey): number {
  if (a.epoch !== b.epoch) return a.epoch < b.epoch ? -1 : 1;
  if (a.slot !== b.slot) return a.slot - b.slot;
  if (a.rank !== b.rank) return b.rank - a.rank;
  if (a.msg !== b.msg) return a.msg - b.msg;
  return a.block - b.block;
}

export function sortByItemId(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return [...items].sort((a, b) => {
    const ka = orderKey(a.item_id);
    const kb = orderKey(b.item_id);
    if (ka === null) return kb === null ? 0 : 1;
    if (kb === null) return -1;
    return compareKeys(ka, kb);
  });
}

function isStandingPromptItem(it: BackendTimelineItem): boolean {
  if (it.item_id === "0.0") return true;
  if (it.kind !== "system_prompt") return false;
  const parts = parseItemIdParts(it.item_id);
  return parts !== null && parts.rank === 0 && parts.msg === 0;
}

export function isReattachedTimelineContext(item: BackendTimelineItem): boolean {
  return (
    isStandingPromptItem(item) ||
    item.kind === "system_prompt" ||
    item.kind === "inbound_compact_summary"
  );
}

function isStandingHeadNoteTag(source: string | null | undefined): boolean {
  switch (source) {
    case "exec_timeout":
    case "timezone":
    case "memory":
    case "agent_id":
    case "agent_memory":
    case "inherited_memory":
    case "preloaded_skills":
      return true;
    default:
      return false;
  }
}

export function standingHeadNoteIds(items: readonly BackendTimelineItem[]): Set<string> {
  const ids = new Set<string>();
  const promptIdx = items.findIndex(isStandingPromptItem);
  if (promptIdx < 0) return ids;
  let i = promptIdx + 1;
  while (
    i < items.length &&
    items[i].kind === "system_marker" &&
    isStandingHeadNoteTag(items[i].source)
  ) {
    ids.add(items[i].item_id);
    i += 1;
  }
  return ids;
}

export function isEventForThread(
  ev: { readonly agent_id: number },
  activeThreadId: number | null,
): boolean {

  if (ev.agent_id === 0) return true;

  return ev.agent_id === activeThreadId;
}
