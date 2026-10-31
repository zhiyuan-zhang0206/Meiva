import { useEffect, type RefObject } from "react";

import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { isAtBottom, type StickyController, type StickyThresholds } from "@/lib/layout/sticky";

import { rememberTimelinePosition, watchReadingNavigation } from "../model/reading-position";

function isInsideActivityScroll(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('[data-activity-scroll="true"]') !== null;
}

export function useTimelineViewport({
  wrapperRef,
  viewportRef,
  contentRef,
  controller,
  stickyThresholds,
  holdRestoredPositionRef,
  pendingRestoreRef,
  restoreProbeRef,
  prependEchoRef,
  coldFillRef,
  scrollMemoryRef,
  stuckRafRef,
  restoreInFlight,
  measureAtBottom,
  maybeColdFill,
  pinToBottom,
  trimFollowing,
  updateStuckHeader,
  prepareHistoryPull,
  historyPullActive,
  pullScroll,
  pullScrollEnd,
  pullWheel,
  pullTouchStart,
  pullTouchMove,
  pullTouchEnd,
  cancelPullTouch,
  applyPendingRestore,
  settleRestoredPosition,
}: {
  wrapperRef: RefObject<HTMLDivElement | null>;
  viewportRef: RefObject<HTMLElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
  controller: StickyController;
  stickyThresholds: StickyThresholds;
  holdRestoredPositionRef: RefObject<boolean>;
  pendingRestoreRef: RefObject<SavedScroll | null>;
  restoreProbeRef: RefObject<{ height: number; passes: number }>;
  prependEchoRef: RefObject<boolean>;
  coldFillRef: RefObject<{ pages: number; open: boolean; inFlight: boolean; sawLoading: boolean }>;
  scrollMemoryRef: RefObject<{ entryKey: string; contentKey: string } | null>;
  stuckRafRef: RefObject<number | null>;
  restoreInFlight: () => boolean;
  measureAtBottom: () => void;
  maybeColdFill: () => void;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  trimFollowing: () => void;
  updateStuckHeader: () => void;
  prepareHistoryPull: (viewport: HTMLElement) => void;
  historyPullActive: () => boolean;
  pullScroll: (viewport: HTMLElement) => boolean;
  pullScrollEnd: () => void;
  pullWheel: (e: WheelEvent, viewport: HTMLElement) => void;
  pullTouchStart: (e: TouchEvent, viewport: HTMLElement) => void;
  pullTouchMove: (e: TouchEvent, viewport: HTMLElement) => void;
  pullTouchEnd: () => void;
  cancelPullTouch: () => void;
  applyPendingRestore: (viewport: HTMLElement | null) => void;
  settleRestoredPosition: (viewport: HTMLElement | null, layoutChanged: boolean) => void;
}) {
  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    if (!viewport) return;
    viewportRef.current = viewport;
    const snapshot = () => ({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });

    const onScroll = () => {
      const boundaryCorrection = pullScroll(viewport);
      if (historyPullActive()) { updateStuckHeader(); return; }
      const direction = holdRestoredPositionRef.current && !pendingRestoreRef.current ? "none" : controller.handleScroll(snapshot());
      if (direction === "up") coldFillRef.current.open = false;

      const isPrependEcho = prependEchoRef.current || boundaryCorrection;
      prependEchoRef.current = false;

      // meiva:note
      // The reader moved: that movement is the position from here on, so the bookmark and the hold that protected it are both spent. Keeping the hold alive here silenced the controller (holding reads every scroll as "none"), which is how a restored reader who then scrolled to the bottom stopped following for good.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
      if (direction !== "none" && !isPrependEcho) {
        pendingRestoreRef.current = null;
        holdRestoredPositionRef.current = false;
      }
      measureAtBottom();
      trimFollowing();
      const mem = scrollMemoryRef.current;
      if (mem && !restoreInFlight()) {
        rememberTimelinePosition(viewport, mem.entryKey, mem.contentKey, controller);
      }
      stuckRafRef.current ??= requestAnimationFrame(() => {
        stuckRafRef.current = null;
        updateStuckHeader();
      });
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });
    const onScrollEnd = (event: Event) => { if (event.target === viewport) pullScrollEnd(); };
    viewport.addEventListener("scrollend", onScrollEnd);
    const stopWatchingReadingInput = watchReadingNavigation(viewport, wrapperRef.current, () => { holdRestoredPositionRef.current = false; });

    // meiva:note
    // Wheel and touch gestures inside the fixed-height Live Activity Window
    // belong exclusively to its inner scrollport and must not disengage the
    // outer timeline's sticky-bottom controller or pull older history.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
    const onWheel = (e: WheelEvent) => {
      if (isInsideActivityScroll(e.target)) return;
      if (!e.ctrlKey && e.deltaY !== 0 && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) holdRestoredPositionRef.current = false;
      if (!e.ctrlKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) controller.handleWheel(e.deltaY, snapshot());
      if (e.deltaY !== 0 && !controller.isSticky() && !isAtBottom(snapshot(), stickyThresholds)) pendingRestoreRef.current = null;
      pullWheel(e, viewport);
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });

    let touchInActivityScroll = false;
    const onTouchStart = (e: TouchEvent) => {
      touchInActivityScroll = isInsideActivityScroll(e.target);
      if (touchInActivityScroll) return;
      holdRestoredPositionRef.current = false;
      controller.handleTouchStart();
      pullTouchStart(e, viewport);
    };
    const onTouchMove = (e: TouchEvent) => {
      if (touchInActivityScroll || isInsideActivityScroll(e.target)) return;
      pullTouchMove(e, viewport);
    };

    const onTouchEnd = () => {
      if (touchInActivityScroll) {
        touchInActivityScroll = false;
        return;
      }
      controller.handleTouchEnd(snapshot());
      pullTouchEnd();
    };
    const onTouchCancel = () => {
      if (touchInActivityScroll) {
        touchInActivityScroll = false;
        return;
      }
      controller.handleTouchEnd(snapshot());
      cancelPullTouch();
    };

    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", onTouchCancel, { passive: true });

    const ro = new ResizeObserver(() => {
      const probe = restoreProbeRef.current;
      if (probe.height === viewport.scrollHeight) probe.passes += 1;
      else { probe.height = viewport.scrollHeight; probe.passes = 1; }

      // meiva:note
      // A layout change that only rewrote inline styles reaches here and no
      // commit, so this is the only reading of the height that a later commit
      // could otherwise compare against a stale baseline. The pin below re-seats
      // the reader anyway, which is why this pass records the height and does
      // not declare a shrink.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
      controller.recordContentHeight(snapshot());
      prepareHistoryPull(viewport);
      applyPendingRestore(viewport);
      settleRestoredPosition(viewport, true);
      if (!historyPullActive() && !holdRestoredPositionRef.current && controller.handleLayoutChange(snapshot())) pinToBottom(viewport);
      measureAtBottom();
      maybeColdFill();
      trimFollowing();
    });
    ro.observe(viewport);
    if (contentRef.current) ro.observe(contentRef.current);

    return () => {
      viewport.removeEventListener("scroll", onScroll);
      viewport.removeEventListener("scrollend", onScrollEnd);
      stopWatchingReadingInput();
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("touchstart", onTouchStart);
      viewport.removeEventListener("touchmove", onTouchMove);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchCancel);
      ro.disconnect();
    };
  }, [
    wrapperRef,
    viewportRef,
    contentRef,
    controller,
    stickyThresholds,
    holdRestoredPositionRef,
    pendingRestoreRef,
    restoreProbeRef,
    prependEchoRef,
    coldFillRef,
    scrollMemoryRef,
    stuckRafRef,
    restoreInFlight,
    measureAtBottom,
    maybeColdFill,
    pinToBottom,
    trimFollowing,
    updateStuckHeader,
    prepareHistoryPull,
    historyPullActive,
    pullScroll,
    pullScrollEnd,
    pullWheel,
    pullTouchStart,
    pullTouchMove,
    pullTouchEnd,
    cancelPullTouch,
    applyPendingRestore,
    settleRestoredPosition,
  ]);
}
