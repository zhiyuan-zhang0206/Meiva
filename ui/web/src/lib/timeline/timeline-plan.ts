import type { BackendTimelineItem, SystemEvent } from "../contracts/types";
import {
  isLiveOverlayId,
  isReattachedTimelineContext,
  isSyntheticEpochItemId,
  parseEpochItemId,
  parseItemIdParts,
  standingHeadNoteIds,
} from "../fold";

export type OlderSegmentLoadResult = "loaded" | "exhausted" | "unready" | "aborted" | "failed";

// meiva:note
// Who asked for an older page. Only the reader's own request — the pull gesture
// or the keyboard control — presents the loading strip above the messages; the
// two automatic fills (the cold first-viewport fill, compact-history retention)
// read history nobody pulled for, and presenting them showed a ring the reader
// never triggered (user report 2026-10-29).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
export type OlderLoadOrigin = "reader" | "background";

export function startsTurn(event: SystemEvent): boolean {
  return (
    event.role === "inbound_arrived" ||
    event.role === "llm_keepalive" ||
    event.role === "chat_start" ||
    event.role === "code_start" ||
    event.role === "reasoning_start" ||
    event.role === "exec_start"
  );
}

export function awaitingPickup(
  turnRunning: boolean,
  arrivedInboundIds: readonly number[],
  items: readonly BackendTimelineItem[],
): boolean {
  if (turnRunning || arrivedInboundIds.length === 0) return false;
  return !items.some((it) => it.inbound_id !== null && arrivedInboundIds.includes(it.inbound_id));
}

export function hasUnsettledClock(items: readonly BackendTimelineItem[]): boolean {
  for (const it of items) {
    if (
      (it.reasoningStartedAt != null && it.reasoning_ms == null) ||
      it.codeStartedAt != null ||
      (it.kind === "agent_code" && it.activity_pending === true && it.code_elapsed_ms == null) ||
      (it.execStartedAt != null && it.exec_ms == null) ||
      isLiveOverlayId(it.item_id)
    ) {
      return true;
    }
  }
  return false;
}

export function isStreamingDelta(ev: SystemEvent): boolean {
  return (
    ev.role === "llm_keepalive" ||
    ev.role === "code_delta" ||
    ev.role === "chat_delta" ||
    ev.role === "reasoning_delta" ||
    ev.role === "exec_output_chunk"
  );
}

export function findOldestTimelineCursor(
  items: readonly BackendTimelineItem[],
): BackendTimelineItem | undefined {
  const headNoteIds = standingHeadNoteIds(items);
  const isRealCursor = (it: BackendTimelineItem): boolean => {
    if (isSyntheticEpochItemId(it.item_id)) return true;
    const parts = parseItemIdParts(parseEpochItemId(it.item_id)?.local ?? it.item_id);
    if (parts === null) return false;
    if (headNoteIds.has(it.item_id)) return false;
    return (
      !isReattachedTimelineContext(it) ||
      (parts.rank > 0 && it.kind === "inbound_compact_summary")
    );
  };
  let oldest = items.find(isRealCursor);
  oldest ??= items.find((it, idx) => {
    if (it.kind !== "inbound_compact_summary") return false;
    if (parseItemIdParts(it.item_id)?.rank !== 0) return false;
    return items
      .slice(0, idx)
      .every((prev) => isReattachedTimelineContext(prev) || headNoteIds.has(prev.item_id));
  });
  return oldest;
}

export function computeOlderPageLimit(olderBaseLimit: number, olderFetchCount: number): number {
  return Math.min(olderBaseLimit * Math.pow(2, olderFetchCount), 1000);
}
