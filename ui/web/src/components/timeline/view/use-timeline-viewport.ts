import { useEffect, type RefObject } from "react";

import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { isAtBottom, type StickyController, type StickyThresholds } from "@/lib/layout/sticky";

import { rememberTimelinePosition, watchReadingNavigation } from "../model/reading-position";

export interface ScrollToBottomRide {
  active: boolean;
  snapping: boolean;
  movedScroll: boolean;
  initialTop: number;
  lastTop: number;
  targetHeight: number;
  idleFrames: number;
  settledFrames: number;
  totalFrames: number;
  rafId: number | null;
}

export function createScrollToBottomRide(): ScrollToBottomRide {
  return {
    active: false,
    snapping: false,
    movedScroll: false,
    initialTop: 0,
    lastTop: 0,
    targetHeight: 0,
    idleFrames: 0,
    settledFrames: 0,
    totalFrames: 0,
    rafId: null,
  };
}

function hasTailSpacer(viewport: HTMLElement): boolean {
  return viewport.querySelector('[data-timeline-spacer="after"], [data-timeline-spacer="turn-after"]') !== null;
}

function isPinnedAtTrueBottom(viewport: HTMLElement, height: number): boolean {
  return Math.abs(height - viewport.scrollTop - viewport.clientHeight) <= 1 && !hasTailSpacer(viewport);
}

export function cancelScrollToBottomRide(ride: ScrollToBottomRide): void {
  if (ride.rafId !== null) {
    cancelAnimationFrame(ride.rafId);
    ride.rafId = null;
  }
  ride.active = false;
  ride.snapping = false;
}

export function startScrollToBottomRide({
  ride,
  viewport,
  controller,
  tailHeight,
  pinToBottom,
  measureAtBottom,
  trimFollowing,
}: {
  ride: ScrollToBottomRide;
  viewport: HTMLElement;
  controller: StickyController;
  tailHeight: (viewport: HTMLElement) => number;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  measureAtBottom: () => void;
  trimFollowing: (allowDuringRide?: boolean) => boolean;
}): void {
  cancelScrollToBottomRide(ride);
  const distance = () => tailHeight(viewport) - viewport.scrollTop - viewport.clientHeight;
  const dist = Math.abs(distance());
  if (dist <= 1 && !hasTailSpacer(viewport)) {
    pinToBottom(viewport, true);
    trimFollowing(true);
    return;
  }

  ride.active = true;
  ride.snapping = dist <= 1;
  ride.movedScroll = dist <= 1;
  ride.initialTop = viewport.scrollTop;
  ride.lastTop = viewport.scrollTop;
  ride.targetHeight = tailHeight(viewport);
  ride.idleFrames = 0;
  ride.settledFrames = 0;
  ride.totalFrames = 0;

  const finalizeAtBottom = () => {
    cancelScrollToBottomRide(ride);
    if (Math.abs(distance()) > 0.5) {
      pinToBottom(viewport, true);
    } else {
      controller.notifyPinnedToBottom(
        {
          scrollTop: viewport.scrollTop,
          scrollHeight: tailHeight(viewport),
          clientHeight: viewport.clientHeight,
        },
        true,
      );
    }
    measureAtBottom();
  };

  const tick = () => {
    ride.rafId = null;
    if (!ride.active) return;
    if (!viewport.isConnected) {
      cancelScrollToBottomRide(ride);
      return;
    }
    ride.totalFrames += 1;
    if (!controller.isSticky()) controller.requestStick();

    const currentTop = viewport.scrollTop;
    if (Math.abs(currentTop - ride.initialTop) > 0.5) {
      ride.movedScroll = true;
    }

    if (isPinnedAtTrueBottom(viewport, tailHeight(viewport))) {
      ride.snapping = true;
      ride.settledFrames += 1;
      if (ride.settledFrames >= 2) {
        if (trimFollowing(true)) {
          ride.settledFrames = 0;
          pinToBottom(viewport, true);
          ride.lastTop = viewport.scrollTop;
          ride.rafId = requestAnimationFrame(tick);
          return;
        }
        finalizeAtBottom();
        return;
      }
      ride.lastTop = currentTop;
      ride.rafId = requestAnimationFrame(tick);
      return;
    }

    ride.settledFrames = 0;

    if (
      !ride.snapping &&
      ride.totalFrames >= 8 &&
      (hasTailSpacer(viewport) || Math.abs(tailHeight(viewport) - ride.targetHeight) > 4)
    ) {
      ride.snapping = true;
      pinToBottom(viewport, true);
      ride.lastTop = viewport.scrollTop;
      ride.idleFrames = 0;
      ride.rafId = requestAnimationFrame(tick);
      return;
    }

    if (Math.abs(currentTop - ride.lastTop) > 0.5) {
      ride.idleFrames = 0;
      ride.lastTop = currentTop;
    } else {
      ride.idleFrames += 1;
      const stallThreshold = ride.movedScroll ? 3 : 12;
      if (ride.snapping || ride.idleFrames >= stallThreshold) {
        ride.snapping = true;
        const beforePin = viewport.scrollTop;
        pinToBottom(viewport, true);
        ride.lastTop = viewport.scrollTop;
        ride.idleFrames = 0;
        if (Math.abs(viewport.scrollTop - beforePin) <= 0.5 && ride.totalFrames >= 30) {
          cancelScrollToBottomRide(ride);
          measureAtBottom();
          trimFollowing(true);
          return;
        }
      }
    }

    if (ride.totalFrames >= 240) {
      finalizeAtBottom();
      trimFollowing(true);
      return;
    }

    ride.rafId = requestAnimationFrame(tick);
  };

  ride.rafId = requestAnimationFrame(tick);
  if (dist > 1) {
    viewport.scrollTo({ top: Math.max(0, tailHeight(viewport) - viewport.clientHeight), behavior: "smooth" });
  } else {
    pinToBottom(viewport, true);
    ride.lastTop = viewport.scrollTop;
  }
}

export function useTimelineViewport({
  wrapperRef,
  viewportRef,
  contentRef,
  controller,
  stickyThresholds,
  scrollToBottomRideRef,
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
  maintainSendAnchor,
  readingReserve,
  tailHeight,
  cancelPendingJump,
}: {
  wrapperRef: RefObject<HTMLDivElement | null>;
  viewportRef: RefObject<HTMLElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
  controller: StickyController;
  stickyThresholds: StickyThresholds;
  scrollToBottomRideRef: RefObject<ScrollToBottomRide>;
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
  trimFollowing: (allowDuringRide?: boolean) => boolean;
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
  // meiva:note
  // Rewrites the send anchor's bottom reserve before the follow rule reads the
  // layout: the reserve is a height the stream consumes, so it belongs to this
  // pass and not to a later one, where the reader would see the reserve and the
  // growth fight for one frame.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  maintainSendAnchor: (viewport: HTMLElement) => boolean;
  readingReserve: () => number;
  tailHeight: (viewport: HTMLElement) => number;
  cancelPendingJump: () => void;
}) {
  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    if (!viewport) return;
    viewportRef.current = viewport;
    const snapshot = () => ({
      scrollTop: viewport.scrollTop,
      scrollHeight: controller.isSticky() ? tailHeight(viewport) : viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });

    const onScroll = () => {
      const boundaryCorrection = pullScroll(viewport);
      if (historyPullActive()) return;
      const padded = readingReserve() > 0;
      const ride = scrollToBottomRideRef.current;
      if (padded && controller.isSticky() && !ride.active &&
          viewport.scrollTop > tailHeight(viewport) - viewport.clientHeight + 1) {
        controller.notifyAnchored({ ...snapshot(), scrollHeight: viewport.scrollHeight });
      }
      // meiva:note
      // A parked row owns its position until reader input releases it. Resize
      // and reserve changes can dispatch scroll before the resize observer.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
      const parked = controller.isAnchored() && maintainSendAnchor(viewport);
      const direction = parked || (holdRestoredPositionRef.current && !pendingRestoreRef.current)
        ? "none" : controller.handleScroll(snapshot(), padded);
      if (direction === "up" && !scrollToBottomRideRef.current.active) coldFillRef.current.open = false;

      if (scrollToBottomRideRef.current.active) {
        scrollToBottomRideRef.current.movedScroll = true;
        if (!controller.isSticky()) controller.requestStick();
        if (isPinnedAtTrueBottom(viewport, tailHeight(viewport))) {
          scrollToBottomRideRef.current.snapping = true;
          controller.notifyPinnedToBottom(snapshot(), true);
        }
      }

      const isPrependEcho = prependEchoRef.current || boundaryCorrection;
      prependEchoRef.current = false;

      // meiva:note
      // The reader moved: that movement is the position from here on, so the bookmark and the hold that protected it are both spent. Keeping the hold alive here silenced the controller (holding reads every scroll as "none"), which is how a restored reader who then scrolled to the bottom stopped following for good.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
      if (direction !== "none" && !isPrependEcho) {
        pendingRestoreRef.current = null;
        holdRestoredPositionRef.current = false;
      }
      measureAtBottom();
      trimFollowing();
      const mem = scrollMemoryRef.current;
      if (mem && !restoreInFlight()) {
        rememberTimelinePosition(viewport, mem.entryKey, mem.contentKey, controller, readingReserve() > 0);
      }
    };
    viewport.addEventListener("scroll", onScroll, { passive: true });
    const onScrollEnd = (event: Event) => {
      if (event.target !== viewport) return;
      pullScrollEnd();
      const ride = scrollToBottomRideRef.current;
      if (ride.active) {
        ride.snapping = true;
        if (!isPinnedAtTrueBottom(viewport, tailHeight(viewport))) {
          pinToBottom(viewport, true);
          ride.lastTop = viewport.scrollTop;
          ride.idleFrames = 0;
          ride.settledFrames = 0;
        } else {
          controller.notifyPinnedToBottom(snapshot(), true);
        }
      }
    };
    viewport.addEventListener("scrollend", onScrollEnd);
    const interruptJump = () => {
      cancelPendingJump();
      if (scrollToBottomRideRef.current.active) {
        cancelScrollToBottomRide(scrollToBottomRideRef.current);
        viewport.scrollTo({ top: viewport.scrollTop, behavior: "instant" });
        controller.notifyAnchored(snapshot());
        measureAtBottom();
      }
    };
    const stopWatchingReadingInput = watchReadingNavigation(viewport, wrapperRef.current, () => {
      interruptJump();
      holdRestoredPositionRef.current = false;
    });

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && e.deltaY !== 0 && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
        holdRestoredPositionRef.current = false;
        if (Math.abs(e.deltaY) > 2) cancelPendingJump();
        if (e.deltaY < -2) {
          interruptJump();
          if (controller.isSticky()) {
            controller.notifyAnchored(snapshot());
            measureAtBottom();
          }
        }
      }
      if (e.deltaY !== 0 && !controller.isSticky() && !isAtBottom(snapshot(), stickyThresholds)) pendingRestoreRef.current = null;
      pullWheel(e, viewport);
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });

    const onTouchStart = (e: TouchEvent) => {
      interruptJump();
      holdRestoredPositionRef.current = false;
      controller.handleTouchStart();
      pullTouchStart(e, viewport);
    };
    const onTouchMove = (e: TouchEvent) => {
      pullTouchMove(e, viewport);
    };

    const onTouchEnd = () => {
      controller.handleTouchEnd(snapshot());
      pullTouchEnd();
    };
    const onTouchCancel = () => {
      controller.handleTouchEnd(snapshot());
      cancelPullTouch();
    };

    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", onTouchCancel, { passive: true });

    const ro = new ResizeObserver(() => {
      maintainSendAnchor(viewport);
      const probe = restoreProbeRef.current;
      if (probe.height === viewport.scrollHeight) probe.passes += 1;
      else { probe.height = viewport.scrollHeight; probe.passes = 1; }

      // meiva:note
      // A layout change that only rewrote inline styles reaches here and no
      // commit, so this is the only reading of the height that a later commit
      // could otherwise compare against a stale baseline. The pin below re-seats
      // the reader anyway, which is why this pass records the height and does
      // not declare a shrink.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
      controller.recordContentHeight(snapshot());
      prepareHistoryPull(viewport);
      applyPendingRestore(viewport);
      settleRestoredPosition(viewport, true);
      const ride = scrollToBottomRideRef.current;
      const shouldPinFromLayout =
        !historyPullActive() && !holdRestoredPositionRef.current && controller.handleLayoutChange(snapshot(), readingReserve() > 0);
      if (ride.active) {
        if (ride.snapping) {
          pinToBottom(viewport, true);
          ride.lastTop = viewport.scrollTop;
          ride.settledFrames = 0;
        }
      } else if (shouldPinFromLayout) {
        pinToBottom(viewport);
      }
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
    scrollToBottomRideRef,
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
    maintainSendAnchor,
    readingReserve,
    tailHeight,
    cancelPendingJump,
  ]);
}
