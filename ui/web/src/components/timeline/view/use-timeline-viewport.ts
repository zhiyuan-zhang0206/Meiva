import { useEffect, type RefObject } from "react";

import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { isAtBottom, type StickyController, type StickyThresholds } from "@/lib/layout/sticky";

import { rememberTimelinePosition, watchReadingNavigation } from "../model/reading-position";

function findScrollableTurnViewport(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null;
  const turnScroll = target.closest<HTMLElement>('[data-turn-scroll="true"]');
  if (!turnScroll) return null;
  const innerViewport = turnScroll.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
  if (!innerViewport || innerViewport.scrollHeight <= innerViewport.clientHeight + 1) return null;
  return innerViewport;
}

function canScrollViewportInDirection(viewport: HTMLElement, deltaY: number): boolean {
  if (deltaY < 0) return viewport.scrollTop > 0;
  if (deltaY > 0) return viewport.scrollTop + viewport.clientHeight < viewport.scrollHeight - 1;
  return false;
}

function normalizeWheelDeltaY(event: WheelEvent, pageHeight: number): number {
  if (event.deltaMode === 1) return event.deltaY * 16;
  if (event.deltaMode === 2) return event.deltaY * pageHeight;
  return event.deltaY;
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
  restoreInFlight,
  measureAtBottom,
  maybeColdFill,
  pinToBottom,
  trimFollowing,
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
  restoreInFlight: () => boolean;
  measureAtBottom: () => void;
  maybeColdFill: () => void;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  trimFollowing: () => void;
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

    // meiva:note
    // Cap every expanded TurnBlock's inner ScrollArea at half of the visible
    // timeline message area (excluding the floating HeaderBar when measured).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
    const syncTurnMaxHeight = () => {
      if (!wrapperRef.current || viewport.clientHeight <= 0) return;
      const visibleHeight =
        viewport.clientHeight > BAR_HEIGHT_PX * 2
          ? viewport.clientHeight - BAR_HEIGHT_PX
          : viewport.clientHeight;
      wrapperRef.current.style.setProperty(
        "--timeline-turn-max-h",
        `${Math.max(160, Math.floor(visibleHeight * 0.5))}px`,
      );
    };
    syncTurnMaxHeight();

    const onScroll = () => {
      const boundaryCorrection = pullScroll(viewport);
      if (historyPullActive()) return;
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
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });
    const onScrollEnd = (event: Event) => { if (event.target === viewport) pullScrollEnd(); };
    viewport.addEventListener("scrollend", onScrollEnd);
    const stopWatchingReadingInput = watchReadingNavigation(viewport, wrapperRef.current, () => { holdRestoredPositionRef.current = false; });

    // meiva:note
    // Wheel and touch gestures inside an overflowing expanded TurnBlock scroll
    // its inner scrollport while it still has travel in that direction; once the
    // inner scrollport reaches its top or bottom boundary, subsequent scrolling
    // chains seamlessly to the outer message list (breaking any compositor wheel
    // latch on the inner viewport and updating the outer sticky-bottom controller).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
    const onWheel = (e: WheelEvent) => {
      const innerViewport = findScrollableTurnViewport(e.target);
      if (innerViewport) {
        if (e.ctrlKey || e.deltaY === 0 || Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
        const pixelDeltaY = normalizeWheelDeltaY(e, viewport.clientHeight);
        if (canScrollViewportInDirection(innerViewport, pixelDeltaY)) return;
        holdRestoredPositionRef.current = false;
        controller.handleWheel(pixelDeltaY, snapshot());
        if (!controller.isSticky() && !isAtBottom(snapshot(), stickyThresholds)) {
          pendingRestoreRef.current = null;
        }
        if (canScrollViewportInDirection(viewport, pixelDeltaY)) {
          if (e.cancelable) {
            e.preventDefault();
            viewport.scrollTop += pixelDeltaY;
          }
          return;
        }
        pullWheel(e, viewport);
        return;
      }
      if (!e.ctrlKey && e.deltaY !== 0 && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) holdRestoredPositionRef.current = false;
      if (!e.ctrlKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) controller.handleWheel(e.deltaY, snapshot());
      if (e.deltaY !== 0 && !controller.isSticky() && !isAtBottom(snapshot(), stickyThresholds)) pendingRestoreRef.current = null;
      pullWheel(e, viewport);
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });

    let touchInnerViewport: HTMLElement | null = null;
    let touchStartY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchInnerViewport = findScrollableTurnViewport(e.target);
      touchStartY = e.touches.length > 0 ? e.touches[0].clientY : null;
      if (touchInnerViewport) return;
      holdRestoredPositionRef.current = false;
      controller.handleTouchStart();
      pullTouchStart(e, viewport);
    };
    const onTouchMove = (e: TouchEvent) => {
      if (touchInnerViewport) {
        const currentY = e.touches.length > 0 ? e.touches[0].clientY : touchStartY;
        const deltaY = touchStartY !== null && currentY !== null ? touchStartY - currentY : 0;
        if (deltaY === 0 || canScrollViewportInDirection(touchInnerViewport, deltaY)) return;
        touchInnerViewport = null;
        holdRestoredPositionRef.current = false;
        controller.handleTouchStart();
        pullTouchStart(e, viewport);
      }
      pullTouchMove(e, viewport);
    };

    const onTouchEnd = () => {
      touchStartY = null;
      if (touchInnerViewport) {
        touchInnerViewport = null;
        return;
      }
      controller.handleTouchEnd(snapshot());
      pullTouchEnd();
    };
    const onTouchCancel = () => {
      touchStartY = null;
      if (touchInnerViewport) {
        touchInnerViewport = null;
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
      syncTurnMaxHeight();
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
    restoreInFlight,
    measureAtBottom,
    maybeColdFill,
    pinToBottom,
    trimFollowing,
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
