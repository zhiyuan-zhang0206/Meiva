import { useLayoutEffect, useRef, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { saveScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import type { TimelinePositionControl } from "@/lib/timeline/reading/position-control";
import type { BookmarkState } from "./bookmark-restore";
import { resolveSavedTimelineAnchor } from "./use-timeline-window";

// meiva:note
// Leaving a conversation always saves the visible item and offset, including
// at the tail or between a stream chunk and its pin. Returning continues that
// reading position while newer output remains below it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function rememberTimelinePosition(
  viewport: HTMLElement, entryKey: string, contentKey: string,
  collapseReserve = 0, keepSendReserve = false,
  control?: { reading: TimelinePositionControl; items: readonly BackendTimelineItem[] },
) {
  if (control && !control.reading.writable) return;
  const reserveMemory = keepSendReserve ? { keepSendReserve: true as const } : {};
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    if (control && control.items.find((value) => value.item_id === item.dataset.itemId)?.kind === "system_prompt") return false;
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  const position: SavedScroll = {
    contentKey, scrollTop: viewport.scrollTop, followBottom: false, ...reserveMemory,
    ...(collapseReserve > 0 && viewport.scrollTop + viewport.clientHeight > viewport.scrollHeight - collapseReserve + 1
      ? { usedCollapseReserve: true as const } : {}),
    anchor: node?.dataset.itemId ? {
      itemId: node.dataset.itemId, rank: Number(node.dataset.displayRank ?? 0),
      viewportTop: node.getBoundingClientRect().top - rect.top,
    } : undefined,
  };
  if (control) {
    const item = control.items.find((value) => value.item_id === position.anchor?.itemId) ?? null;
    control.reading.remember(position, item);
  } else saveScrollMemory(entryKey, position);
}

export function useReadingPositionControl(control: TimelinePositionControl | undefined, { pending: pendingRef, settled: settledRef, hold: holdRef, probe: probeRef }: BookmarkState) {
  const current = useRef(control);
  const token = control?.token;
  useLayoutEffect(() => { current.current = control; });
  useLayoutEffect(() => {
    const value = current.current;
    if (!value) return;
    pendingRef.current = value.position;
    settledRef.current = null;
    holdRef.current = value.position !== null;
    probeRef.current = { height: -1, passes: 0 };
  }, [token, pendingRef, settledRef, holdRef, probeRef]);
  return current;
}

export function notifyReadingRestore(
  control: RefObject<TimelinePositionControl | undefined>,
  state: BookmarkState,
  viewport: HTMLElement | null,
  items: readonly BackendTimelineItem[],
) {
  const saved = state.settled.current ?? state.pending.current;
  if (!control.current?.ready || !saved?.anchor || !viewport) return;
  const item = items.find((value) => value.item_id === saved.anchor?.itemId);
  if (!item?.reading_locator || item.reading_locator !== saved.anchor.readingLocator) return;
  const target = resolveSavedTimelineAnchor(viewport, saved.anchor, null, items);
  if (target.node && Math.abs(target.node.getBoundingClientRect().top -
    viewport.getBoundingClientRect().top - target.viewportTop) <= 1) control.current.restored();
}

interface ReadingExpansion {
  items: ReadonlyMap<string, boolean>;
  turns: ReadonlyMap<string, boolean>;
}
// Only ephemeral view choices live here. Server rows stay in the QueryClient.
const expansions = new Map<string, ReadingExpansion>();
export function readTimelineExpansion(key: string | undefined) {
  return key ? expansions.get(key) : undefined;
}
export function rememberTimelineExpansion(key: string | undefined, value: ReadingExpansion) {
  if (key) expansions.set(key, value);
}


/** Scroll keys and scrollbar drags resume normal following after a bookmark. */
export function watchReadingNavigation(viewport: HTMLElement, wrapper: HTMLElement | null, resume: () => void) {
  const keyDown = (event: KeyboardEvent) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) resume();
  };
  const pointerDown = (event: PointerEvent) => {
    const el = event.target as HTMLElement | null;
    if (el?.closest('[data-slot="scroll-area-scrollbar"]') && !el.closest('[data-activity-scroll="true"]')) {
      resume();
    }
  };
  viewport.addEventListener("keydown", keyDown);
  wrapper?.addEventListener("pointerdown", pointerDown, { capture: true });
  return () => {
    viewport.removeEventListener("keydown", keyDown);
    wrapper?.removeEventListener("pointerdown", pointerDown, { capture: true });
  };
}
