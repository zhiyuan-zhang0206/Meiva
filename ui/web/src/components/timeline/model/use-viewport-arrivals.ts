import { useCallback, useLayoutEffect, useRef, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { StickyController } from "@/lib/layout/sticky";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

// meiva:note
// Where the viewport goes when the READER asks for something, rather than when
// the stream grows: a thread change pins to the bottom, a send pins to the
// bottom and re-enables following so the reply keeps streaming into view. Both
// are placements the follow flag has to learn about, and both release a
// bookmark that was still settling - the reader picked a position, so the saved
// one no longer owns the viewport.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

export interface ThreadPinArgs {
  threadKey: string | undefined;
  controller: StickyController;
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  anchorRef: RefObject<{ id: string; rank: number; frontId: string | null; docTop: number } | null>;
  pendingRef: RefObject<SavedScroll | null>;
  settledRef: RefObject<SavedScroll | null>;
  holdRef: RefObject<boolean>;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  setAtBottom: (value: boolean) => void;
}

export function useThreadPin({
  threadKey, viewportRef, wrapperRef, anchorRef,
  pendingRef, settledRef, holdRef, pinToBottom, setAtBottom,
}: ThreadPinArgs): void {
  const last = useRef<{ thread: string | undefined } | null>(null);
  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
      ?? null;
    const previous = last.current;
    if (previous !== null && previous.thread === threadKey) return;
    const pending = pendingRef.current;
    if (pending !== null && pending.contentKey === threadKey) {
      last.current = { thread: threadKey };
      return;
    }
    last.current = { thread: threadKey };
    anchorRef.current = null;
    pendingRef.current = null;
    settledRef.current = null;
    holdRef.current = false;
    setAtBottom(true);
    if (viewport) pinToBottom(viewport, true);
  }, [pinToBottom, setAtBottom, threadKey, viewportRef, wrapperRef, anchorRef, pendingRef, settledRef, holdRef]);
}

// meiva:note
// The send anchor: when the reader sends a message, pin the viewport to the
// bottom and re-enable sticky following, and re-pin once the committed row
// mounts so the timeline stays glued to the bottom instead of releasing the
// scroll-to-bottom state (user request 2026-11-09).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export interface SendAnchorArgs {
  items: readonly BackendTimelineItem[];
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  reserveRef: RefObject<HTMLDivElement | null>;
  controller: StickyController;
  measureAtBottom: () => void;
  cancelBookmark: () => void;
}

function snapshotOf(viewport: HTMLElement) {
  return {
    scrollTop: viewport.scrollTop,
    scrollHeight: viewport.scrollHeight,
    clientHeight: viewport.clientHeight,
  };
}

function reserveHeight(element: HTMLElement): number {
  return element.offsetHeight;
}

// meiva:note
// Which committed row a send anchor belongs to: the inbound the receipt named,
// or, when the receipt carried none, the newest user message the timeline shows.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
function targetItemId(
  items: readonly BackendTimelineItem[],
  inboundId: number | null,
): string | null {
  if (inboundId !== null) {
    return items.find((item) => item.inbound_id === inboundId)?.item_id ?? null;
  }
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index].kind === "inbound_chat") return items[index].item_id;
  }
  return null;
}

function findRow(viewport: HTMLElement, itemId: string): HTMLElement | null {
  const escaped = CSS.escape(itemId);
  return viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="' + escaped + '"]')
    ?? viewport.querySelector<HTMLElement>('[data-turn-expanded="false"][data-item-id="' + escaped + '"]');
}

export function useSendAnchor({
  items,
  viewportRef,
  wrapperRef,
  reserveRef,
  controller,
  measureAtBottom,
  cancelBookmark,
}: SendAnchorArgs): { maintain: () => void } {
  const request = useTimelineStore((state) => state.sendAnchor);
  const consumedSeq = useRef(request.seq);
  const pending = useRef<number | null | undefined>(undefined);

  const writeReserve = useCallback((height: number) => {
    const element = reserveRef.current;
    if (!element) return;
    const next = Math.max(0, Math.ceil(height));
    if (Math.abs(reserveHeight(element) - next) <= 1) return;
    element.style.height = `${next}px`;
  }, [reserveRef]);

  const maintain = useCallback(() => {
    writeReserve(0);
  }, [writeReserve]);

  const pinSentToBottom = useCallback((viewport: HTMLElement) => {
    cancelBookmark();
    writeReserve(0);
    viewport.scrollTop = viewport.scrollHeight;
    controller.notifyPinnedToBottom(snapshotOf(viewport), true);
    measureAtBottom();
  }, [cancelBookmark, controller, measureAtBottom, writeReserve]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
      ?? null;
    if (!viewport) return;

    if (request.seq !== consumedSeq.current) {
      consumedSeq.current = request.seq;
      pending.current = request.inboundId;
      writeReserve(0);
    }

    if (pending.current !== undefined) {
      const itemId = targetItemId(items, pending.current);
      const node = itemId === null ? null : findRow(viewport, itemId);
      if (node) {
        pending.current = undefined;
        pinSentToBottom(viewport);
      }
    }
    maintain();
  });

  return { maintain };
}
