import type { BackendTimelineItem } from "../contracts/types";

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

export function sortByItemId(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return [...items].sort((a, b) => {
    const pa = parseItemIdParts(a.item_id);
    const pb = parseItemIdParts(b.item_id);

    if (pa === null) return pb === null ? 0 : 1;
    if (pb === null) return -1;

    const aHarness = a.item_id.startsWith("h");
    const bHarness = b.item_id.startsWith("h");
    if (aHarness !== bHarness) {
      if (a.kind === "system_prompt" || a.item_id === "0.0") return -1;
      if (b.kind === "system_prompt" || b.item_id === "0.0") return 1;
      if (a.created_at && b.created_at) {
        const cmp = a.created_at.localeCompare(b.created_at);
        if (cmp !== 0) return cmp;
      } else if (a.created_at && !b.created_at) {
        return 1;
      } else if (!a.created_at && b.created_at) {
        return -1;
      }
      return aHarness ? 1 : -1;
    }

    if (pa.rank !== pb.rank) return pb.rank - pa.rank;
    if (pa.msg !== pb.msg) return pa.msg - pb.msg;
    return pa.block - pb.block;
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

export function standingHeadNoteIds(items: readonly BackendTimelineItem[]): Set<string> {
  const ids = new Set<string>();
  const promptIdx = items.findIndex(isStandingPromptItem);
  if (promptIdx < 0) return ids;
  let i = promptIdx + 1;
  while (i < items.length && items[i].kind === "system_marker" && items[i].source != null) {
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
