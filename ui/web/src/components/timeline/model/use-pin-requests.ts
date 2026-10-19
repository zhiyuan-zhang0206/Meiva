import { useLayoutEffect, useRef, type RefObject } from "react";

import { bottomZone, isAtBottom, type StickyController, type StickyThresholds } from "@/lib/layout/sticky";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

// meiva:note
// The send / agent-switch pin, split out of the timeline view. A send re-pins a reader who is following the tail - the sticky flag, or, while a bookmark is still settling, the measured bottom of a page that actually scrolls; a parked reader is never moved. A thread change pins unless that thread's own bookmark owns the mount. A pin cancels any bookmark in flight: the reader asked for the newest message, so the bookmark no longer owns the viewport.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md

export interface PinRequestArgs {
  threadKey: string | undefined;
  controller: StickyController;
  thresholds: StickyThresholds;
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  anchorRef: RefObject<{ id: string; rank: number; frontId: string | null; docTop: number } | null>;
  pendingRef: RefObject<SavedScroll | null>;
  settledRef: RefObject<SavedScroll | null>;
  holdRef: RefObject<boolean>;
  restoreInFlight: () => boolean;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  setAtBottom: (value: boolean) => void;
}

const scrolledPage = (viewport: HTMLElement, thresholds: StickyThresholds) =>
  viewport.scrollHeight - viewport.clientHeight > bottomZone(viewport.clientHeight, thresholds);

export function usePinRequests({
  threadKey, controller, thresholds, viewportRef, wrapperRef, anchorRef,
  pendingRef, settledRef, holdRef, restoreInFlight, pinToBottom, setAtBottom,
}: PinRequestArgs): void {
  const request = useTimelineStore((state) => state.scrollToBottomRequest);
  const last = useRef<{ request: number; thread: string | undefined } | null>(null);
  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
      ?? null;
    const atTail = viewport !== null && scrolledPage(viewport, thresholds) && isAtBottom({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    }, thresholds);
    const previous = last.current;
    const sameThread = previous !== null && previous.thread === threadKey;
    if (previous !== null && previous.thread === threadKey && previous.request === request) return;
    const isSend = sameThread;
    if (!isSend && pendingRef.current?.contentKey === threadKey) {
      last.current = { request, thread: threadKey };
      return;
    }
    if (isSend && (restoreInFlight() ? !atTail : !controller.isSticky())) return;

    last.current = { request, thread: threadKey };
    anchorRef.current = null;
    pendingRef.current = null;
    settledRef.current = null;
    holdRef.current = false;
    setAtBottom(true);
    if (viewport) pinToBottom(viewport, true);
  }, [request, controller, pinToBottom, restoreInFlight, setAtBottom, thresholds, threadKey, viewportRef, wrapperRef, anchorRef, pendingRef, settledRef, holdRef]);
}
