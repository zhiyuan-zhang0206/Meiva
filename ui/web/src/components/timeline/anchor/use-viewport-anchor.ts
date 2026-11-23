"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { bottomZone, type StickyThresholds } from "@/lib/layout/sticky";

import { expandedTurnHeader, findRowNode, offsetOf, READING_TOP_PX, readRowAnchor, rowOf, TAIL, type RowRef, type ViewAnchor } from "./dom";
import { rideClock, rideDuration, rideProgress } from "./ride";

// meiva:note
// The one writer of the timeline's scrollTop. The anchor is the reader's place; after every commit
// and every layout change (ResizeObserver on the viewport and the content) the writer puts the
// anchor row back at its offset, or the viewport on the natural end for the tail. Prepends,
// compaction, width changes, images and late heights therefore need no code of their own. Only the
// reader moves the anchor: a scroll the writer did not cause is read back from the DOM. Placements
// (return, reload, reveal, quote, toggle, the arrow) set it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

export type AnchorChangeCause = "reader" | "placement";

// meiva:note
// A row inside a collapsed turn is shown through the block: keep the block below the header bar.
const COLLAPSED_MIN_OFFSET_PX = BAR_HEIGHT_PX + 8;
const TAIL_ROUNDING_PX = 1;
const TOUCH_UPWARD_INTENT_PX = 2;
const TOUCH_LIFTOFF_REVERSAL_PX = 4;

interface Args {
  wrapperRef: RefObject<HTMLDivElement | null>;
  viewportRef: RefObject<HTMLDivElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
  overlaySlackRef?: RefObject<HTMLDivElement | null>;
  reserveRef: RefObject<HTMLDivElement | null>;
  thresholds: StickyThresholds;
  onChange: (anchor: ViewAnchor, cause: AnchorChangeCause) => void;
  // meiva:note
  // The anchor row is not mounted after a commit; the caller decides whether data has it.
  onUnresolved: (row: RowRef) => void;
  pinnedHead?: (id: string) => boolean;
  // meiva:note
  // The reader stands in rows that are not mounted: nothing should pin the window elsewhere.
  onFree?: () => void;
  // meiva:note
  // The arrow's ride arrived at the tail.
  onRideEnd?: () => void;
  viewportObserverFactory?: (callback: ResizeObserverCallback) => ResizeObserver;
}

export interface ViewportAnchor {
  // meiva:note
  // The scrolling element, or null before it mounts.
  viewport: () => HTMLElement | null;
  // meiva:note
  // The anchor the writer holds, or null before the first placement.
  current: () => ViewAnchor | null;
  // meiva:note
  // Set the reader's place. `smooth` animates a reader-requested move (the arrow).
  place: (anchor: ViewAnchor, options?: { smooth?: boolean; hold?: boolean }) => void;
  // meiva:note
  // Apply the anchor: the view calls this after every commit, once placement has run.
  apply: () => void;
  holdReserve: () => boolean;
  // meiva:note
  // Upward pull intent at the top: the reader is reading history, not the tail.
  releaseTail: () => void;
  // meiva:note
  // Move to a scroll offset the reader asked for (the end of the reserve) and read the place back.
  placeScrollTop: (top: number) => void;
  // meiva:note
  // Height of the reserve below the natural end.
  reserveHeight: () => number;
  overlaySlackHeight: () => number;
  // meiva:note
  // The arrow's smooth ride to the tail is under way.
  riding: () => boolean;
  // meiva:note
  // The reader follows the tail or has reached the dock-safe end of a bounded return.
  atTail: () => boolean;
  // meiva:note
  // Rename the anchor row in place (a compact re-ranks it) without moving the viewport. Until the
  // view commits the re-keyed rows (rekeySettled), a row that cannot be found is not reported lost.
  rekey: (row: RowRef) => void;
  rekeySettled: () => void;
}

export function useViewportAnchor({
  wrapperRef, viewportRef, contentRef, overlaySlackRef, reserveRef, thresholds, onChange, onUnresolved, pinnedHead, onFree, onRideEnd,
  viewportObserverFactory,
}: Args): { controls: ViewportAnchor; atEnd: boolean } {
  const anchorRef = useRef<ViewAnchor | null>(null);
  const lastWrittenRef = useRef<number | null>(null);
  const directionTopRef = useRef<number | null>(null);
  const touchingRef = useRef(false);
  const touchRef = useRef<{
    x: number | null;
    y: number | null;
    up: number;
    startTop: number;
    minTop: number;
    maxTop: number;
  } | null>(null);
  const rideRef = useRef<{ frame: number; from: number; start: number; duration: number } | null>(null);
  const holdRef = useRef(false);
  const inOverlaySlackRef = useRef(false);
  const prevOverlaySlackRef = useRef(0);
  const [atEnd, setAtEnd] = useState(true);
  // meiva:note
  // The reader is in rows that are not mounted (a scrollbar or Home jump into a virtual spacer):
  // nothing is written until rows mount there and the place can be read.
  const freeRef = useRef(false);
  const rekeyPendingRef = useRef(false);
  const towardEndRef = useRef(true);
  const latest = useRef({ thresholds, onChange, onUnresolved, pinnedHead, onFree, onRideEnd });
  useLayoutEffect(() => {
    latest.current = { thresholds, onChange, onUnresolved, pinnedHead, onFree, onRideEnd };
  });
  const readRow = useCallback((vp: HTMLElement) => readRowAnchor(vp, latest.current.pinnedHead), []);

  const viewport = useCallback(
    () => viewportRef.current ?? wrapperRef.current?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]') ?? null,
    [viewportRef, wrapperRef],
  );
  const overlaySlackHeight = useCallback(() => overlaySlackRef?.current?.offsetHeight ?? 0, [overlaySlackRef]);
  const reserveHeight = useCallback(() => reserveRef.current?.offsetHeight ?? 0, [reserveRef]);
  // meiva:note
  // The scroll offset at which the content's last row meets the viewport bottom: the tail.
  const naturalEnd = useCallback((vp: HTMLElement, clearance = 0) => {
    const content = contentRef.current;
    const contentEnd = content
      ? vp.scrollTop + content.getBoundingClientRect().bottom - vp.getBoundingClientRect().top
      : vp.scrollHeight - overlaySlackHeight() - reserveHeight();
    return Math.max(0, contentEnd + clearance - vp.clientHeight);
  }, [contentRef, overlaySlackHeight, reserveHeight]);
  const boundedEnd = useCallback((vp: HTMLElement) => naturalEnd(vp, overlaySlackHeight()), [naturalEnd, overlaySlackHeight]);

  // meiva:note
  // The arrow hides at the scrollable end, inside overlay scroll slack past the resting end, and
  // inside the bottom zone of the natural end.
  const refreshEnd = useCallback((vp: HTMLElement) => {
    const distance = vp.scrollHeight - vp.scrollTop - vp.clientHeight;
    const natural = naturalEnd(vp) - vp.scrollTop;
    const pastNaturalInOverlay = natural < -0.5 && reserveHeight() === 0;
    const next = anchorRef.current?.kind === "bounded-tail" ? boundedEnd(vp) - vp.scrollTop <= 1 :
      rideRef.current !== null || distance <= 1 || pastNaturalInOverlay ||
      (natural >= -0.5 && natural <= bottomZone(vp.clientHeight, latest.current.thresholds));
    setAtEnd((previous) => (previous === next ? previous : next));
  }, [boundedEnd, naturalEnd, reserveHeight]);

  // meiva:note
  // The reserve is derived, never accumulated: while a header toggle holds it, it is exactly the space
  // below the content the toggled header needs to stay where it was clicked, measured within the
  // content. The tail never needs one. An anchor row that is not mounted keeps the current height.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  const syncReserve = useCallback((vp: HTMLElement) => {
    const reserve = reserveRef.current;
    const content = contentRef.current;
    if (!reserve || !content) return;
    const anchor = anchorRef.current;
    let need = 0;
    if (holdRef.current && anchor?.kind === "row") {
      const hit = findRowNode(vp, anchor.row);
      if (!hit) {
        need = reserve.offsetHeight;
      } else {
        const box = content.getBoundingClientRect();
        const offset = hit.collapsed ? Math.max(COLLAPSED_MIN_OFFSET_PX, anchor.offset) : anchor.offset;
        need = hit.node.getBoundingClientRect().top - box.top - offset + vp.clientHeight - box.height - overlaySlackHeight();
      }
    }
    const next = `${Math.max(0, Math.ceil(need))}px`;
    if (reserve.style.height !== next) reserve.style.height = next;
  }, [contentRef, overlaySlackHeight, reserveRef]);

  const write = useCallback((vp: HTMLElement, top: number) => {
    const before = vp.scrollTop;
    const max = Math.max(0, vp.scrollHeight - vp.clientHeight);
    const target = Math.min(Math.max(0, top), max);
    if (Math.abs(before - target) >= 0.5) vp.scrollTop = target;
    const after = vp.scrollTop;
    const shift = after - before;
    if (shift !== 0 && touchRef.current) {
      touchRef.current.startTop += shift;
      touchRef.current.minTop += shift;
      touchRef.current.maxTop += shift;
    }
    lastWrittenRef.current = after;
    directionTopRef.current = after;
  }, []);

  const setAnchor = useCallback((anchor: ViewAnchor, cause: AnchorChangeCause) => {
    const previous = anchorRef.current;
    anchorRef.current = anchor;
    if ((previous?.kind === "bounded-tail") !== (anchor.kind === "bounded-tail")) {
      viewport()?.toggleAttribute("data-unread-bounded", anchor.kind === "bounded-tail");
    }
    const same = previous?.kind === anchor.kind && (anchor.kind === "tail" ||
      (previous.kind !== "tail" && previous.row === anchor.row &&
        (anchor.kind === "bounded-tail" || (previous.kind === "row" && previous.offset === anchor.offset && previous.clearTurnHeader === anchor.clearTurnHeader))));
    if (!same) latest.current.onChange(anchor, cause);
  }, [viewport]);

  const apply = useCallback(() => {
    const vp = viewport();
    if (vp && freeRef.current && !touchingRef.current) {
      const row = readRow(vp);
      if (row) {
        freeRef.current = false;
        setAnchor(row, "reader");
      }
      lastWrittenRef.current = vp.scrollTop;
      directionTopRef.current = vp.scrollTop;
      refreshEnd(vp);
      return;
    }
    let anchor = anchorRef.current;
    // meiva:note
    // While a finger is down it owns the position: the tail is not pinned under it. A row anchor still
    // takes the finger's movement as the reader's and compensates only rows changing above it (an
    // older page landing mid-drag), so content never jumps under the finger.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    if (!vp || !anchor || rideRef.current || (touchingRef.current && anchor.kind !== "row")) {
      if (vp) refreshEnd(vp);
      return;
    }
    // meiva:note
    // A position the writer did not leave is the reader's, even before its scroll event arrives (a
    // smooth wheel animation advances between events): the row moved with it, so its offset takes
    // that movement and only what is left over (rows changing above it) is compensated. Writing the
    // stale offset back would cancel the reader's scroll mid-gesture on every streamed commit.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    // meiva:note
    // A browser clamp (content shrank, or the viewport grew, under the last position) is not the
    // reader's: the anchor keeps its offset and the reserve or the writer restores it.
    const max = Math.max(0, vp.scrollHeight - vp.clientHeight);
    const last = lastWrittenRef.current;
    const clamped = last !== null && last > max + 0.5 && Math.abs(vp.scrollTop - max) < 0.5;
    const drift = last === null || clamped ? 0 : vp.scrollTop - last;
    if ((anchor.kind === "bounded-tail" || (anchor.kind === "row" && anchor.clearTurnHeader)) && Math.abs(drift) >= 0.5) {
      const row = readRow(vp);
      if (row) setAnchor(row, "reader");
      else {
        freeRef.current = true;
        anchorRef.current = null;
        vp.removeAttribute("data-unread-bounded");
        latest.current.onFree?.();
      }
      lastWrittenRef.current = vp.scrollTop;
      refreshEnd(vp);
      return;
    }
    const currentOverlay = overlaySlackHeight();
    const overlayShrank = currentOverlay < prevOverlaySlackRef.current - 0.5;
    prevOverlaySlackRef.current = currentOverlay;
    if (inOverlaySlackRef.current && anchor.kind === "row" && !holdRef.current && overlayShrank) {
      if (currentOverlay <= 0.5 || (clamped && vp.scrollTop <= naturalEnd(vp) + 1)) {
        inOverlaySlackRef.current = false;
        anchor = TAIL;
        setAnchor(TAIL, "reader");
        lastWrittenRef.current = vp.scrollTop;
        directionTopRef.current = vp.scrollTop;
      } else if (clamped) {
        const row = readRow(vp);
        if (row) {
          anchor = row;
          setAnchor(row, "reader");
        }
        lastWrittenRef.current = vp.scrollTop;
        directionTopRef.current = vp.scrollTop;
      }
    } else if (anchor.kind === "row" && Math.abs(drift) >= 0.5) {
      anchor = { kind: "row", row: anchor.row, offset: anchor.offset - drift };
      anchorRef.current = anchor;
      lastWrittenRef.current = vp.scrollTop;
      directionTopRef.current = vp.scrollTop;
      if (drift < -0.5) towardEndRef.current = false;
      else if (drift > 0.5) towardEndRef.current = true;
    }
    syncReserve(vp);
    if (anchor.kind === "tail") {
      write(vp, naturalEnd(vp));
    } else {
      const hit = findRowNode(vp, anchor.row);
      if (!hit) {
        if (!rekeyPendingRef.current) latest.current.onUnresolved(anchor.row);
        refreshEnd(vp);
        return;
      }
      // meiva:note
      // Found under another identity (a compact re-ranked it, older rows merged into its turn): the
      // anchor takes the identity the row has now, so the next re-key can still trace it.
      const current = rowOf(hit.node, anchor.row, hit.collapsed);
      if (current !== anchor.row) setAnchor({ ...anchor, row: current }, "reader");
      const clearTurn = anchor.kind === "bounded-tail" || anchor.clearTurnHeader;
      const headerHeight = clearTurn ? expandedTurnHeader(hit.node)?.getBoundingClientRect().height ?? 0 : 0;
      const preferredOffset = (anchor.kind === "bounded-tail" ? READING_TOP_PX : anchor.offset) + headerHeight;
      const offset = hit.collapsed ? Math.max(COLLAPSED_MIN_OFFSET_PX, preferredOffset) : preferredOffset;
      const rowTop = vp.scrollTop + offsetOf(vp, hit.node) - offset;
      write(vp, anchor.kind === "bounded-tail" ? Math.min(boundedEnd(vp), rowTop) : rowTop);
    }
    refreshEnd(vp);
  }, [boundedEnd, naturalEnd, overlaySlackHeight, readRow, refreshEnd, setAnchor, syncReserve, viewport, write]);

  const finishRide = useCallback(() => {
    const ride = rideRef.current;
    if (!ride) return;
    rideClock.cancel(ride.frame);
    rideRef.current = null;
    apply();
    latest.current.onRideEnd?.();
  }, [apply]);

  const cancelRide = useCallback(() => {
    const ride = rideRef.current;
    if (!ride) return;
    rideClock.cancel(ride.frame);
    rideRef.current = null;
  }, []);

  // meiva:note
  // The reader reaches the tail only at the natural end, within rounding error. Inside overlay
  // slack or toggle reserve, a row holds their place. The arrow's visibility zone is independent.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const derive = useCallback((vp: HTMLElement) => {
    const distance = naturalEnd(vp) - vp.scrollTop;
    const pastNatural = distance < -0.5;
    if (!pastNatural && distance <= TAIL_ROUNDING_PX) {
      freeRef.current = false;
      inOverlaySlackRef.current = false;
      setAnchor(TAIL, "reader");
      return;
    }
    const row = readRow(vp);
    if (row) {
      freeRef.current = false;
      const inSlack = pastNatural && reserveHeight() === 0 && overlaySlackHeight() > 0;
      inOverlaySlackRef.current = inSlack;
      if (inSlack) prevOverlaySlackRef.current = overlaySlackHeight();
      setAnchor(row, "reader");
    } else if (!freeRef.current) {
      // meiva:note
      // A jump into rows that are not mounted: hold no place (nothing to write, no tail to follow)
      // until rows mount there and can be read.
      freeRef.current = true;
      inOverlaySlackRef.current = false;
      anchorRef.current = null;
      vp.removeAttribute("data-unread-bounded");
      latest.current.onFree?.();
    }
  }, [naturalEnd, overlaySlackHeight, readRow, reserveHeight, setAnchor]);

  const releaseTailAt = useCallback((vp: HTMLElement) => {
    const current = anchorRef.current;
    if (!current || current.kind === "row" || (current.kind === "bounded-tail" && vp.scrollTop <= 0.5)) return;
    const row = readRow(vp);
    if (!row) return;
    inOverlaySlackRef.current = false;
    lastWrittenRef.current = vp.scrollTop;
    directionTopRef.current = vp.scrollTop;
    setAnchor(row, "reader");
  }, [readRow, setAnchor]);

  const releaseTail = useCallback(() => {
    const vp = viewport();
    if (vp) releaseTailAt(vp);
  }, [releaseTailAt, viewport]);

  const place = useCallback((anchor: ViewAnchor, options: { smooth?: boolean; hold?: boolean } = {}) => {
    const vp = viewport();
    cancelRide();
    if (options.hold !== undefined) holdRef.current = options.hold;
    freeRef.current = false;
    inOverlaySlackRef.current = false;
    setAnchor(anchor, "placement");
    if (!vp) return;
    // meiva:note
    // A placement replaces the reader's place outright: no movement since the last write is theirs.
    lastWrittenRef.current = vp.scrollTop;
    directionTopRef.current = vp.scrollTop;
    // meiva:note
    // The ride steers toward the live natural end every frame, so rows that mount or re-measure on
    // the way (virtual spacers) move the goal, never the deadline.
    if (options.smooth && anchor.kind === "tail") {
      syncReserve(vp);
      const target = naturalEnd(vp);
      if (Math.abs(vp.scrollTop - target) > 1) {
        const ride = { frame: 0, from: vp.scrollTop, start: rideClock.now(), duration: rideDuration(target - vp.scrollTop) };
        const tick = () => {
          if (rideRef.current !== ride) return;
          const progress = rideProgress(rideClock.now() - ride.start, ride.duration);
          write(vp, ride.from + (naturalEnd(vp) - ride.from) * progress);
          if (progress >= 1) finishRide();
          else ride.frame = rideClock.frame(tick);
        };
        rideRef.current = ride;
        ride.frame = rideClock.frame(tick);
        refreshEnd(vp);
        return;
      }
    }
    apply();
  }, [apply, cancelRide, finishRide, naturalEnd, refreshEnd, setAnchor, syncReserve, viewport, write]);

  const releaseReserve = useCallback((vp: HTMLElement) => {
    if (!holdRef.current) return;
    holdRef.current = false;
    syncReserve(vp);
  }, [syncReserve]);

  useEffect(() => {
    const vp = viewport();
    if (!vp) return;
    directionTopRef.current = vp.scrollTop;
    const onScroll = () => {
      const top = vp.scrollTop;
      if (rideRef.current) {
        directionTopRef.current = top;
        return;
      }
      if (lastWrittenRef.current !== null && Math.abs(top - lastWrittenRef.current) < 1) {
        refreshEnd(vp);
        return;
      }
      const base = directionTopRef.current ?? top;
      const delta = top - base;
      if (delta < -0.5) {
        towardEndRef.current = false;
        directionTopRef.current = top;
      } else if (delta > 0.5) {
        towardEndRef.current = true;
        directionTopRef.current = top;
      }
      const gesture = touchRef.current;
      if (gesture) {
        if (top < gesture.minTop) gesture.minTop = top;
        if (top > gesture.maxTop) gesture.maxTop = top;
      }
      // meiva:note
      // Moving upward to or past the natural end spends the reserve; movement inside it keeps it.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
      if (!towardEndRef.current && top <= naturalEnd(vp) + 0.5) releaseReserve(vp);
      // meiva:note
      // Under a finger the movement stays unattributed: the writer folds it into the row anchor's
      // offset (keeping rows that change above in place) and the release reads the place back.
      if (touchingRef.current) {
        refreshEnd(vp);
        return;
      }
      derive(vp);
      lastWrittenRef.current = vp.scrollTop;
      refreshEnd(vp);
    };
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.deltaY === 0 || Math.abs(event.deltaY) < Math.abs(event.deltaX)) return;
      cancelRide();
      // meiva:note
      // An upward notch leaves the tail before its scroll event: a chunk landing first must not pin.
      if (event.deltaY < -2) {
        towardEndRef.current = false;
        releaseTailAt(vp);
      }
    };
    const onInput = (event: Event) => {
      if (event instanceof KeyboardEvent && !["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) return;
      cancelRide();
    };
    const onTouchStart = (event: TouchEvent) => {
      touchingRef.current = true;
      cancelRide();
      const top = vp.scrollTop;
      directionTopRef.current = top;
      const finger = event.touches.length === 1 ? event.touches[0] : null;
      touchRef.current = {
        x: finger && Number.isFinite(finger.clientX) ? finger.clientX : null,
        y: finger && Number.isFinite(finger.clientY) ? finger.clientY : null,
        up: 0,
        startTop: top,
        minTop: top,
        maxTop: top,
      };
    };
    const onTouchMove = (event: TouchEvent) => {
      const gesture = touchRef.current;
      if (!gesture || event.touches.length !== 1 || gesture.y === null) {
        if (gesture) {
          gesture.x = null;
          gesture.y = null;
          gesture.up = 0;
        }
        return;
      }
      const finger = event.touches[0];
      if (!Number.isFinite(finger.clientY)) return;
      const nextX = Number.isFinite(finger.clientX) ? finger.clientX : (gesture.x ?? 0);
      const nextY = finger.clientY;
      const stepX = gesture.x === null ? 0 : nextX - gesture.x;
      const stepY = nextY - gesture.y;
      gesture.x = nextX;
      gesture.y = nextY;
      if (stepY > 0 && Math.abs(stepY) >= Math.abs(stepX)) {
        gesture.up += stepY;
      } else if (stepY < 0) {
        gesture.up = 0;
      }
      if (gesture.up > TOUCH_UPWARD_INTENT_PX && vp.scrollTop <= naturalEnd(vp) + 0.5) {
        towardEndRef.current = false;
        releaseTailAt(vp);
      }
    };
    const onTouchEnd = () => {
      touchingRef.current = false;
      const gesture = touchRef.current;
      touchRef.current = null;
      const top = vp.scrollTop;
      if (gesture) {
        const minTop = Math.min(gesture.minTop, top);
        const maxTop = Math.max(gesture.maxTop, top);
        if (top < gesture.startTop - 0.5 && top - minTop < TOUCH_LIFTOFF_REVERSAL_PX) {
          towardEndRef.current = false;
        } else if (top > gesture.startTop + 0.5 && maxTop - top < TOUCH_LIFTOFF_REVERSAL_PX) {
          towardEndRef.current = true;
        }
      }
      if (anchorRef.current?.kind === "bounded-tail" && gesture &&
          Math.max(gesture.maxTop, top) - Math.min(gesture.minTop, top) <= 0.5) {
        apply();
        return;
      }
      derive(vp);
      lastWrittenRef.current = vp.scrollTop;
      directionTopRef.current = vp.scrollTop;
      refreshEnd(vp);
    };
    vp.addEventListener("scroll", onScroll, { passive: true });
    vp.addEventListener("wheel", onWheel, { passive: true });
    vp.addEventListener("keydown", onInput);
    vp.addEventListener("pointerdown", onInput);
    vp.addEventListener("touchstart", onTouchStart, { passive: true });
    vp.addEventListener("touchmove", onTouchMove, { passive: true });
    vp.addEventListener("touchend", onTouchEnd, { passive: true });
    vp.addEventListener("touchcancel", onTouchEnd, { passive: true });
    const scrollbar = wrapperRef.current;
    const onScrollbar = (event: PointerEvent) => {
      if ((event.target as HTMLElement | null)?.closest('[data-slot="scroll-area-scrollbar"]')) cancelRide();
    };
    scrollbar?.addEventListener("pointerdown", onScrollbar, { capture: true });
    const observer = viewportObserverFactory ? viewportObserverFactory(() => apply()) : new ResizeObserver(() => apply());
    observer.observe(vp);
    if (contentRef.current) observer.observe(contentRef.current);
    return () => {
      vp.removeEventListener("scroll", onScroll);
      vp.removeEventListener("wheel", onWheel);
      vp.removeEventListener("keydown", onInput);
      vp.removeEventListener("pointerdown", onInput);
      vp.removeEventListener("touchstart", onTouchStart);
      vp.removeEventListener("touchmove", onTouchMove);
      vp.removeEventListener("touchend", onTouchEnd);
      vp.removeEventListener("touchcancel", onTouchEnd);
      scrollbar?.removeEventListener("pointerdown", onScrollbar, { capture: true });
      observer.disconnect();
      cancelRide();
    };
  }, [apply, cancelRide, contentRef, derive, naturalEnd, refreshEnd, releaseReserve, releaseTailAt, viewport, viewportObserverFactory, wrapperRef]);

  const placeScrollTop = useCallback((top: number) => {
    const vp = viewport();
    if (!vp) return;
    cancelRide();
    write(vp, top);
    derive(vp);
    refreshEnd(vp);
  }, [cancelRide, derive, refreshEnd, viewport, write]);

  const current = useCallback(() => anchorRef.current, []);
  const holdReserve = useCallback(() => holdRef.current, []);
  const riding = useCallback(() => rideRef.current !== null, []);
  const rekey = useCallback((row: RowRef) => {
    const current = anchorRef.current;
    if (!current || current.kind === "tail") return;
    anchorRef.current = { ...current, row };
    rekeyPendingRef.current = true;
  }, []);
  const rekeySettled = useCallback(() => { rekeyPendingRef.current = false; }, []);
  const atTail = useCallback(() => {
    const vp = viewport();
    return anchorRef.current?.kind === "tail" ||
      (anchorRef.current?.kind === "bounded-tail" && vp !== null && boundedEnd(vp) - vp.scrollTop <= TAIL_ROUNDING_PX);
  }, [boundedEnd, viewport]);
  const controls = useMemo<ViewportAnchor>(() => ({
    viewport, current, place, apply, holdReserve, releaseTail, placeScrollTop, reserveHeight, overlaySlackHeight, riding, atTail, rekey, rekeySettled,
  }), [apply, atTail, current, holdReserve, overlaySlackHeight, place, placeScrollTop, rekey, rekeySettled, releaseTail, reserveHeight, riding, viewport]);
  return { controls, atEnd };
}
