"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { bottomZone, type StickyThresholds } from "@/lib/layout/sticky";

import { findRowNode, offsetOf, readRowAnchor, rowOf, TAIL, type RowRef, type ViewAnchor } from "./dom";

// meiva:note
// The one writer of the timeline's scrollTop. The anchor is the reader's place; after every commit
// and every layout change (ResizeObserver on the viewport and the content) the writer puts the
// anchor row back at its offset, or the viewport on the natural end for the tail. Prepends,
// compaction, width changes, images and late heights therefore need no code of their own. Only the
// reader moves the anchor: a scroll the writer did not cause is read back from the DOM. Placements
// (return, reload, reveal, send, toggle, the arrow) set it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

export type AnchorChangeCause = "reader" | "placement";

// meiva:note
// A row inside a collapsed turn is shown through the block: keep the block below the header bar.
const COLLAPSED_MIN_OFFSET_PX = BAR_HEIGHT_PX + 8;
// meiva:note
// The native smooth scroll has no completion event where `scrollend` is missing (WebKit): a ride
// that stops moving for this many frames has arrived.
const RIDE_STILL_FRAMES = 3;

interface Args {
  wrapperRef: RefObject<HTMLDivElement | null>;
  viewportRef: RefObject<HTMLDivElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
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
  armSendReserve: (armed: boolean) => void;
  sendReserveArmed: () => boolean;
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
  // meiva:note
  // The arrow's smooth ride to the tail is under way.
  riding: () => boolean;
}

export function useViewportAnchor({
  wrapperRef, viewportRef, contentRef, reserveRef, thresholds, onChange, onUnresolved, pinnedHead, onFree, onRideEnd,
  viewportObserverFactory,
}: Args): { controls: ViewportAnchor; atEnd: boolean } {
  const anchorRef = useRef<ViewAnchor | null>(null);
  const lastWrittenRef = useRef<number | null>(null);
  const touchingRef = useRef(false);
  const rideRef = useRef<{ frame: number; still: number; top: number } | null>(null);
  const sendArmedRef = useRef(false);
  const holdRef = useRef(false);
  const [atEnd, setAtEnd] = useState(true);
  // meiva:note
  // The reader is in rows that are not mounted (a scrollbar or Home jump into a virtual spacer):
  // nothing is written until rows mount there and the place can be read.
  const freeRef = useRef(false);
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
  const reserveHeight = useCallback(() => reserveRef.current?.offsetHeight ?? 0, [reserveRef]);
  // meiva:note
  // The scroll offset at which the content's last row meets the viewport bottom: the tail.
  const naturalEnd = useCallback((vp: HTMLElement) => {
    const content = contentRef.current;
    if (!content) return Math.max(0, vp.scrollHeight - reserveHeight() - vp.clientHeight);
    return Math.max(0, vp.scrollTop + content.getBoundingClientRect().bottom - vp.getBoundingClientRect().top - vp.clientHeight);
  }, [contentRef, reserveHeight]);

  // meiva:note
  // The arrow hides at the scrollable end and inside the bottom zone of the natural end.
  const refreshEnd = useCallback((vp: HTMLElement) => {
    const distance = vp.scrollHeight - vp.scrollTop - vp.clientHeight;
    const natural = naturalEnd(vp) - vp.scrollTop;
    const next = rideRef.current !== null || distance <= 1 ||
      (natural >= -0.5 && natural <= bottomZone(vp.clientHeight, latest.current.thresholds));
    setAtEnd((previous) => (previous === next ? previous : next));
  }, [naturalEnd]);

  // meiva:note
  // The reserve is derived, never accumulated: while a send or a header toggle allows it, it is
  // exactly the space below the content the current row anchor needs to sit at its offset, measured
  // within the content so the bottom alignment of a short column does not feed back into it. The tail
  // never needs one. An anchor row that is not mounted keeps the current height.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  const syncReserve = useCallback((vp: HTMLElement) => {
    const reserve = reserveRef.current;
    const content = contentRef.current;
    if (!reserve || !content) return;
    const anchor = anchorRef.current;
    let need = 0;
    if ((sendArmedRef.current || holdRef.current) && anchor?.kind === "row") {
      const hit = findRowNode(vp, anchor.row);
      if (!hit) {
        need = reserve.offsetHeight;
      } else {
        const box = content.getBoundingClientRect();
        const offset = hit.collapsed ? Math.max(COLLAPSED_MIN_OFFSET_PX, anchor.offset) : anchor.offset;
        need = hit.node.getBoundingClientRect().top - box.top - offset + vp.clientHeight - box.height;
      }
    }
    const next = `${Math.max(0, Math.ceil(need))}px`;
    if (reserve.style.height !== next) reserve.style.height = next;
  }, [contentRef, reserveRef]);

  const write = useCallback((vp: HTMLElement, top: number) => {
    const max = Math.max(0, vp.scrollHeight - vp.clientHeight);
    const target = Math.min(Math.max(0, top), max);
    if (Math.abs(vp.scrollTop - target) >= 0.5) vp.scrollTop = target;
    lastWrittenRef.current = vp.scrollTop;
  }, []);

  const setAnchor = useCallback((anchor: ViewAnchor, cause: AnchorChangeCause) => {
    const previous = anchorRef.current;
    anchorRef.current = anchor;
    const same = previous !== null && previous.kind === anchor.kind &&
      (anchor.kind === "tail" || (previous.kind === "row" && previous.row === anchor.row && previous.offset === anchor.offset));
    if (!same) latest.current.onChange(anchor, cause);
  }, []);

  const apply = useCallback(() => {
    const vp = viewport();
    if (vp && freeRef.current && !touchingRef.current) {
      const row = readRow(vp);
      if (row) {
        freeRef.current = false;
        setAnchor(row, "reader");
      }
      lastWrittenRef.current = vp.scrollTop;
      refreshEnd(vp);
      return;
    }
    let anchor = anchorRef.current;
    if (!vp || !anchor || touchingRef.current || rideRef.current) {
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
    if (anchor.kind === "row" && Math.abs(drift) >= 0.5) {
      anchor = { kind: "row", row: anchor.row, offset: anchor.offset - drift };
      anchorRef.current = anchor;
      lastWrittenRef.current = vp.scrollTop;
    }
    syncReserve(vp);
    if (anchor.kind === "tail") {
      write(vp, naturalEnd(vp));
    } else {
      const hit = findRowNode(vp, anchor.row);
      if (!hit) {
        latest.current.onUnresolved(anchor.row);
        refreshEnd(vp);
        return;
      }
      // meiva:note
      // Found under another identity (a compact re-ranked it, older rows merged into its turn): the
      // anchor takes the identity the row has now, so the next re-key can still trace it.
      const current = rowOf(hit.node, anchor.row, hit.collapsed);
      if (current !== anchor.row) setAnchor({ kind: "row", row: current, offset: anchor.offset }, "reader");
      const offset = hit.collapsed ? Math.max(COLLAPSED_MIN_OFFSET_PX, anchor.offset) : anchor.offset;
      write(vp, vp.scrollTop + offsetOf(vp, hit.node) - offset);
    }
    refreshEnd(vp);
  }, [naturalEnd, readRow, refreshEnd, setAnchor, syncReserve, viewport, write]);

  const finishRide = useCallback(() => {
    const ride = rideRef.current;
    if (!ride) return;
    cancelAnimationFrame(ride.frame);
    rideRef.current = null;
    apply();
    latest.current.onRideEnd?.();
  }, [apply]);

  const cancelRide = useCallback((vp: HTMLElement) => {
    const ride = rideRef.current;
    if (!ride) return;
    cancelAnimationFrame(ride.frame);
    rideRef.current = null;
    vp.scrollTo({ top: vp.scrollTop, behavior: "instant" });
  }, []);

  // meiva:note
  // The reader's place is read back from the DOM: the tail inside the bottom zone of the natural
  // end (never while standing in the reserve), otherwise the first readable row and its offset.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  // meiva:note
  // `towardEnd`: the reader's last movement went down (or nowhere). Only that reaches the tail inside
  // the bottom zone (bounce or momentum stopping short of the end); a reader moving up stays where
  // they stopped unless they are exactly at the end.
  const derive = useCallback((vp: HTMLElement, towardEnd: boolean) => {
    const natural = naturalEnd(vp);
    const inReserve = reserveHeight() > 0 && vp.scrollTop > natural + 0.5;
    const distance = natural - vp.scrollTop;
    if (!inReserve && (distance <= 1 || (towardEnd && distance <= bottomZone(vp.clientHeight, latest.current.thresholds)))) {
      freeRef.current = false;
      setAnchor(TAIL, "reader");
      return;
    }
    const row = readRow(vp);
    if (row) {
      freeRef.current = false;
      setAnchor(row, "reader");
    } else if (!freeRef.current) {
      // meiva:note
      // A jump into rows that are not mounted: hold no place (nothing to write, no tail to follow)
      // until rows mount there and can be read.
      freeRef.current = true;
      anchorRef.current = null;
      latest.current.onFree?.();
    }
  }, [naturalEnd, readRow, reserveHeight, setAnchor]);

  const releaseTail = useCallback(() => {
    const vp = viewport();
    if (!vp || anchorRef.current?.kind !== "tail") return;
    const row = readRow(vp);
    if (row) setAnchor(row, "reader");
  }, [readRow, setAnchor, viewport]);

  const place = useCallback((anchor: ViewAnchor, options: { smooth?: boolean; hold?: boolean } = {}) => {
    const vp = viewport();
    if (vp) cancelRide(vp);
    if (options.hold !== undefined) holdRef.current = options.hold;
    freeRef.current = false;
    setAnchor(anchor, "placement");
    if (!vp) return;
    // meiva:note
    // A placement replaces the reader's place outright: no movement since the last write is theirs.
    lastWrittenRef.current = vp.scrollTop;
    if (options.smooth && anchor.kind === "tail") {
      syncReserve(vp);
      const target = naturalEnd(vp);
      if (Math.abs(vp.scrollTop - target) > 1) {
        lastWrittenRef.current = target;
        const tick = () => {
          const ride = rideRef.current;
          if (!ride) return;
          ride.still = Math.abs(vp.scrollTop - ride.top) < 0.5 ? ride.still + 1 : 0;
          ride.top = vp.scrollTop;
          if (ride.still >= RIDE_STILL_FRAMES) finishRide();
          else ride.frame = requestAnimationFrame(tick);
        };
        rideRef.current = { frame: requestAnimationFrame(tick), still: 0, top: vp.scrollTop };
        vp.scrollTo({ top: target, behavior: "smooth" });
        refreshEnd(vp);
        return;
      }
    }
    apply();
  }, [apply, cancelRide, finishRide, naturalEnd, refreshEnd, setAnchor, syncReserve, viewport]);

  const releaseReserves = useCallback((vp: HTMLElement) => {
    if (!sendArmedRef.current && !holdRef.current) return;
    sendArmedRef.current = false;
    holdRef.current = false;
    syncReserve(vp);
  }, [syncReserve]);

  useEffect(() => {
    const vp = viewport();
    if (!vp) return;
    let previousTop = vp.scrollTop;
    const onScroll = () => {
      const top = vp.scrollTop;
      const up = top < previousTop - 0.5;
      previousTop = top;
      if (rideRef.current) return;
      if (lastWrittenRef.current !== null && Math.abs(top - lastWrittenRef.current) < 1) {
        refreshEnd(vp);
        return;
      }
      // meiva:note
      // Moving upward to or past the natural end spends the reserve; movement inside it keeps it.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
      if (up && top <= naturalEnd(vp) + 0.5) releaseReserves(vp);
      towardEndRef.current = !up;
      if (!touchingRef.current) derive(vp, !up);
      lastWrittenRef.current = vp.scrollTop;
      refreshEnd(vp);
    };
    const onScrollEnd = (event: Event) => {
      if (event.target === vp) finishRide();
    };
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.deltaY === 0 || Math.abs(event.deltaY) < Math.abs(event.deltaX)) return;
      cancelRide(vp);
      // meiva:note
      // An upward notch leaves the tail before its scroll event: a chunk landing first must not pin.
      if (event.deltaY < -2 && anchorRef.current?.kind === "tail") {
        const row = readRow(vp);
        if (row) setAnchor(row, "reader");
      }
    };
    const onInput = (event: Event) => {
      if (event instanceof KeyboardEvent && !["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) return;
      cancelRide(vp);
    };
    const onTouchStart = () => {
      touchingRef.current = true;
      cancelRide(vp);
    };
    const onTouchEnd = () => {
      touchingRef.current = false;
      derive(vp, towardEndRef.current);
      lastWrittenRef.current = vp.scrollTop;
    };
    vp.addEventListener("scroll", onScroll, { passive: true });
    vp.addEventListener("scrollend", onScrollEnd);
    vp.addEventListener("wheel", onWheel, { passive: true });
    vp.addEventListener("keydown", onInput);
    vp.addEventListener("pointerdown", onInput);
    vp.addEventListener("touchstart", onTouchStart, { passive: true });
    vp.addEventListener("touchend", onTouchEnd, { passive: true });
    vp.addEventListener("touchcancel", onTouchEnd, { passive: true });
    const scrollbar = wrapperRef.current;
    const onScrollbar = (event: PointerEvent) => {
      if ((event.target as HTMLElement | null)?.closest('[data-slot="scroll-area-scrollbar"]')) cancelRide(vp);
    };
    scrollbar?.addEventListener("pointerdown", onScrollbar, { capture: true });
    const observer = viewportObserverFactory ? viewportObserverFactory(() => apply()) : new ResizeObserver(() => apply());
    observer.observe(vp);
    if (contentRef.current) observer.observe(contentRef.current);
    return () => {
      vp.removeEventListener("scroll", onScroll);
      vp.removeEventListener("scrollend", onScrollEnd);
      vp.removeEventListener("wheel", onWheel);
      vp.removeEventListener("keydown", onInput);
      vp.removeEventListener("pointerdown", onInput);
      vp.removeEventListener("touchstart", onTouchStart);
      vp.removeEventListener("touchend", onTouchEnd);
      vp.removeEventListener("touchcancel", onTouchEnd);
      scrollbar?.removeEventListener("pointerdown", onScrollbar, { capture: true });
      observer.disconnect();
      const ride = rideRef.current;
      if (ride) cancelAnimationFrame(ride.frame);
      rideRef.current = null;
    };
  }, [apply, cancelRide, contentRef, derive, finishRide, naturalEnd, readRow, refreshEnd, releaseReserves, setAnchor, viewport, viewportObserverFactory, wrapperRef]);

  const placeScrollTop = useCallback((top: number) => {
    const vp = viewport();
    if (!vp) return;
    cancelRide(vp);
    write(vp, top);
    derive(vp, true);
    refreshEnd(vp);
  }, [cancelRide, derive, refreshEnd, viewport, write]);

  const current = useCallback(() => anchorRef.current, []);
  const armSendReserve = useCallback((armed: boolean) => { sendArmedRef.current = armed; }, []);
  const sendReserveArmed = useCallback(() => sendArmedRef.current, []);
  const holdReserve = useCallback(() => holdRef.current, []);
  const riding = useCallback(() => rideRef.current !== null, []);
  const controls = useMemo<ViewportAnchor>(() => ({
    viewport, current, place, apply, armSendReserve, sendReserveArmed, holdReserve, releaseTail, placeScrollTop, reserveHeight, riding,
  }), [apply, armSendReserve, current, holdReserve, place, placeScrollTop, releaseTail, reserveHeight, riding, sendReserveArmed, viewport]);
  return { controls, atEnd };
}
