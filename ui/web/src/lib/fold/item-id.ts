import type { BackendTimelineItem } from "../contracts/types";

export function parseItemId(itemId: string): [number, number] | null {
  const parts = itemId.split(".");
  if (parts.length !== 2) return null;
  const msgIdx = Number(parts[0]);
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
  const msg = Number(msgPart);
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

    if (pa.rank !== pb.rank) return pb.rank - pa.rank;
    if (pa.msg !== pb.msg) return pa.msg - pb.msg;
    return pa.block - pb.block;
  });
}

export function isReattachedTimelineContext(item: BackendTimelineItem): boolean {
  return item.kind === "system_prompt" || item.kind === "inbound_compact_summary";
}

export function standingHeadNoteIds(items: readonly BackendTimelineItem[]): Set<string> {
  const ids = new Set<string>();
  const promptIdx = items.findIndex((it) => it.item_id === "0.0");
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
