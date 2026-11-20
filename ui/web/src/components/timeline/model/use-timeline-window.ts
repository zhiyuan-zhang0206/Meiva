"use client";

import { useCallback, useDeferredValue, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useDisplayLimit } from "@/lib/format/display-limits";
import { TIMELINE_TURN_ROW_GAP_PX } from "@/lib/layout/layout";


// meiva:note
// Matches space-y-3 between timeline groups; details use the shared work-row gap.
const GAP = 12;
// KEEP (task #3696 exception inventory): task #4709 fixes one viewport of buffer on each side as window geometry, not an operator limit.
const BUFFER_VIEWPORTS = 1;
// KEEP (task #3696 exception inventory): 24 is roughly three 8-row screens before geometry exists; it caps initial DOM mounts, not a user window.
const BOOTSTRAP_WINDOW_ROWS = 24;
const BOOTSTRAP_PIN_LEAD_ROWS = BOOTSTRAP_WINDOW_ROWS / 2;
// KEEP (task #3696 exception inventory): 500 keys cover twice the default 250-item follower slice before cache sweeping; this is housekeeping.
const HEIGHT_CACHE_SWEEP_FLOOR = 500;
const ACTIVATION_ROWS_FALLBACK = 100;
const TURN_ROWS_FALLBACK = 100;
const MEASURE_ROWS_FALLBACK = 75;

export function useTimelineWindowLimits() {
  const configuredActivationRows = useDisplayLimit("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", ACTIVATION_ROWS_FALLBACK);
  const activationRows = useDeferredValue(configuredActivationRows);
  const turnRows = useDisplayLimit("MEIVA_TIMELINE_WINDOW_TURN_ROWS", TURN_ROWS_FALLBACK);
  // A lower runtime threshold first renders with measurement active, then switches
  // the DOM window on the next commit so the parked row can be pinned.
  const measureRows = Math.min(
    useDisplayLimit("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", MEASURE_ROWS_FALLBACK),
    configuredActivationRows - 1, activationRows - 1,
  );
  return { activationRows, turnRows, measureRows };
}

export interface WindowGroup {
  key: string;
  rank: number;
  itemIds: readonly string[];
  memberRankKeys?: readonly string[];
  estimatedHeight: number;
  expandedRows: readonly string[] | null;
}

export interface WindowRange {
  start: number;
  end: number;
  before: number;
  after: number;
}

function groupHeightKey(group: WindowGroup): string {
  return group.expandedRows ? `${group.key}:open` : group.key;
}

function keepFocusInTimeline(content: HTMLElement, rect: DOMRect): void {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !content.contains(active)) return;
  const row = active.closest<HTMLElement>(".timeline-item, [data-turn-id]");
  if (!row) return;
  const box = row.getBoundingClientRect();
  if (box.bottom >= rect.top - rect.height && box.top <= rect.bottom + rect.height) return;
  content.tabIndex = -1;
  content.focus({ preventScroll: true });
}

export function span(sizes: readonly number[], start: number, end: number, gap = GAP): number {
  if (end <= start) return 0;
  let height = 0;
  for (let index = start; index < end; index++) height += sizes[index] + gap;
  return height - gap;
}

/** The remote rows become two spacers; only the viewport and one viewport on each side mount. */
export function windowForSizes(
  sizes: readonly number[],
  top: number,
  height: number,
  origin = 0,
  gap = GAP,
): WindowRange {
  const min = Math.max(0, top - height * BUFFER_VIEWPORTS - origin);
  const max = Math.max(min, top + height * (1 + BUFFER_VIEWPORTS) - origin);
  let cursor = 0;
  let start = 0;
  while (start < sizes.length - 1 && cursor + sizes[start] + gap < min) {
    cursor += sizes[start] + gap;
    start++;
  }
  let end = start;
  while (end < sizes.length && (cursor < max || end === start)) {
    cursor += sizes[end] + gap;
    end++;
  }
  return {
    start,
    end,
    before: span(sizes, 0, start, gap),
    after: span(sizes, end, sizes.length, gap),
  };
}

interface Options {
  groups: readonly WindowGroup[];
  enabled: boolean;
  turnRows: number;
  measureRows: number;
  identity: string | null;
  viewportRef: { current: HTMLElement | null };
  contentRef: { current: HTMLElement | null };
  isFollowing: () => boolean;
  // meiva:note
  // A row a placement is about to land on, or the reader's row: the window mounts it before the
  // writer reads it.
  pin: { itemId: string; rank: number } | null;
  // meiva:note
  // The reader follows the tail: the window is measured from the end, so moving the tail does not
  // move the window (a window placed from scrollTop and the tail pin would chase each other).
  tail: boolean;
}

export function useTimelineWindow({
  groups,
  enabled,
  turnRows,
  measureRows,
  identity,
  viewportRef,
  contentRef,
  isFollowing,
  pin,
  tail,
}: Options) {
  const [heights, setHeights] = useState(() => new Map<string, number>());
  const previousIdentityRef = useRef(identity);
  const [view, setView] = useState({ top: 0, height: 0, origin: 52 });
  const viewRef = useRef(view);
  const tracking = enabled || groups.reduce((count, group) =>
    count + Math.max(group.itemIds.length, group.expandedRows?.length ?? 1), 0) >= measureRows;
  useLayoutEffect(() => { viewRef.current = view; }, [view]);
  useEffect(() => {
    if (previousIdentityRef.current === identity) return;
    previousIdentityRef.current = identity;
    setHeights(new Map());
  }, [identity]);

  // Live mirrors for the subscriptions below. The scroll listener and both
  // ResizeObservers are installed once per tracking session and read the
  // latest groups / heights / enabled here; keying them on those values
  // re-subscribed (and re-observed every row) on each streamed commit.
  const groupsRef = useRef(groups);
  const heightsRef = useRef(heights);
  const enabledRef = useRef(enabled);
  const isFollowingRef = useRef(isFollowing);
  useLayoutEffect(() => {
    groupsRef.current = groups;
    heightsRef.current = heights;
    enabledRef.current = enabled;
    isFollowingRef.current = isFollowing;
  });

  const measureView = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const content = contentRef.current;
    if (content && enabledRef.current) keepFocusInTimeline(content, rect);
    const groups = groupsRef.current;
    const heights = heightsRef.current;
    const first = content?.querySelector<HTMLElement>("[data-virtual-group]");
    const firstIndex = first ? groups.findIndex((group) => group.key === first.dataset.virtualGroup) : -1;
    let origin = viewRef.current.origin;
    if (first && firstIndex >= 0 && rect.height > 0 && first.getBoundingClientRect().height > 0) {
      origin = first.getBoundingClientRect().top - rect.top + viewport.scrollTop;
      for (let index = 0; index < firstIndex; index++) {
        origin -= (heights.get(groupHeightKey(groups[index])) ?? groups[index].estimatedHeight) + GAP;
      }
    }
    const next = { top: viewport.scrollTop, height: viewport.clientHeight, origin };
    const previous = viewRef.current;
    const changed = Math.abs(previous.top - next.top) >= 1 || previous.height !== next.height ||
      Math.abs(previous.origin - next.origin) >= 1;
    if (changed) {
      viewRef.current = next;
      setView(next);
    }
  }, [contentRef, viewportRef]);

  // A compact handoff, send pin or restore moves scrollTop in an earlier
  // layout effect. Sampling every commit puts that position into the same
  // pre-paint render that clears its pin. The read is cheap on a streamed
  // commit: TimelineView's stuck-header sync has already flushed layout, and
  // measureView only renders again when the view actually moved.
  const measureViewRef = useRef(measureView);
  useLayoutEffect(() => {
    measureViewRef.current = measureView;
    if (tracking) measureView();
  });

  useEffect(() => {
    if (!tracking) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    measureView();
    viewport.addEventListener("scroll", measureView, { passive: true });
    const observer = new ResizeObserver(measureView);
    observer.observe(viewport);
    return () => {
      viewport.removeEventListener("scroll", measureView);
      observer.disconnect();
    };
  }, [tracking, measureView, viewportRef]);

  const sizes = groups.map((group) => heights.get(groupHeightKey(group)) ?? group.estimatedHeight);
  const pinnedIndex = pin
    ? groups.findIndex((group) =>
        group.memberRankKeys
          ? group.memberRankKeys.includes(`${pin.rank}:${pin.itemId}`)
          : group.rank === pin.rank && group.itemIds.includes(pin.itemId),
      )
    : -1;
  const range = (() => {
    if (!enabled) return { start: 0, end: groups.length, before: 0, after: 0 };
    if (view.height <= 0) {
      if (pinnedIndex >= 0) {
        const start = Math.max(0, pinnedIndex - BOOTSTRAP_PIN_LEAD_ROWS);
        const end = Math.min(groups.length, start + BOOTSTRAP_WINDOW_ROWS);
        return { start, end, before: span(sizes, 0, start), after: span(sizes, end, sizes.length) };
      }
      const start = Math.max(0, groups.length - BOOTSTRAP_WINDOW_ROWS);
      return { start, end: groups.length, before: span(sizes, 0, start), after: 0 };
    }
    if (tail) {
      let start = groups.length;
      let cursor = 0;
      while (start > 0 && cursor < view.height * (1 + BUFFER_VIEWPORTS)) {
        start--;
        cursor += sizes[start] + GAP;
      }
      return { start, end: groups.length, before: span(sizes, 0, start), after: 0 };
    }
    let top = view.top;
    if (pinnedIndex >= 0) {
      const pinTop = view.origin + span(sizes, 0, pinnedIndex) + (pinnedIndex ? GAP : 0);
      const pinBottom = pinTop + sizes[pinnedIndex];
      if (pinBottom < top - view.height || pinTop > top + view.height * 2) top = pinTop;
    }
    return windowForSizes(sizes, top, view.height, view.origin);
  })();

  const rowRange = (groupIndex: number): WindowRange => {
    const group = groups[groupIndex];
    const rows = group.expandedRows;
    if (!enabled || !rows || rows.length <= turnRows) {
      return { start: 0, end: rows?.length ?? 0, before: 0, after: 0 };
    }
    const childSizes = rows.map((key) => heights.get(key) ?? 80);
    if (view.height <= 0) {
      const childPin = groupIndex === pinnedIndex && pin
        ? rows.findIndex((key) => key.endsWith(`:${pin.rank}:${pin.itemId}`)) : -1;
      if (childPin >= 0) {
        const start = Math.max(0, childPin - BOOTSTRAP_PIN_LEAD_ROWS);
        const end = Math.min(rows.length, start + BOOTSTRAP_WINDOW_ROWS);
        return { start, end, before: span(childSizes, 0, start, TIMELINE_TURN_ROW_GAP_PX), after: span(childSizes, end, rows.length, TIMELINE_TURN_ROW_GAP_PX) };
      }
      const start = Math.max(0, rows.length - BOOTSTRAP_WINDOW_ROWS);
      return { start, end: rows.length, before: span(childSizes, 0, start, TIMELINE_TURN_ROW_GAP_PX), after: 0 };
    }
    if (tail && groupIndex === groups.length - 1) {
      let start = rows.length;
      let cursor = 0;
      while (start > 0 && cursor < view.height * (1 + BUFFER_VIEWPORTS)) {
        start--;
        cursor += childSizes[start] + TIMELINE_TURN_ROW_GAP_PX;
      }
      return { start, end: rows.length, before: span(childSizes, 0, start, TIMELINE_TURN_ROW_GAP_PX), after: 0 };
    }
    const groupTop = view.origin + span(sizes, 0, groupIndex) + (groupIndex ? GAP : 0);
    const childOrigin = groupTop + 60;
    let top = view.top;
    const childPin = groupIndex === pinnedIndex && pin
      ? rows.findIndex((key) => key.endsWith(`:${pin.rank}:${pin.itemId}`)) : -1;
    if (childPin >= 0) {
      const pinTop = childOrigin + span(childSizes, 0, childPin, TIMELINE_TURN_ROW_GAP_PX) + (childPin ? TIMELINE_TURN_ROW_GAP_PX : 0);
      const pinBottom = pinTop + childSizes[childPin];
      if (pinBottom < top - view.height || pinTop > top + view.height * 2) top = pinTop;
    }
    return windowForSizes(childSizes, top, view.height, childOrigin, TIMELINE_TURN_ROW_GAP_PX);
  };

  // One ResizeObserver per tracking session records each mounted group's and
  // expanded row's height. Each commit only observes newly mounted nodes and
  // releases unmounted ones; re-creating the observer per commit re-observed
  // every row, and each observe() queues a fresh notification.
  const rowObserverRef = useRef<{ observer: ResizeObserver; nodes: Set<HTMLElement> } | null>(null);
  useLayoutEffect(() => {
    if (!tracking) return;
    const content = contentRef.current;
    if (!content) return;
    const observer = new ResizeObserver((entries) => {
      let changed = false;
      const nextHeights = new Map(heightsRef.current);
      for (const entry of entries) {
        const node = entry.target as HTMLElement;
        const groupKey = node.dataset.virtualGroup;
        const key = groupKey
          ? (node.querySelector('[data-turn-expanded="true"]') ? `${groupKey}:open` : groupKey)
          : node.dataset.virtualRow;
        if (!key) continue;
        const height = entry.borderBoxSize[0]?.blockSize ?? node.getBoundingClientRect().height;
        if (height <= 0 || Math.abs((nextHeights.get(key) ?? 0) - height) < 1) continue;
        nextHeights.set(key, height);
        changed = true;
      }
      if (!changed) return;
      const rect = viewportRef.current?.getBoundingClientRect();
      if (rect && enabledRef.current) keepFocusInTimeline(content, rect);
      const groups = groupsRef.current;
      if (nextHeights.size > Math.max(HEIGHT_CACHE_SWEEP_FLOOR, groups.length * 2)) {
        const live = new Set(groups.flatMap((group) => [group.key, `${group.key}:open`, ...(group.expandedRows ?? [])]));
        for (const key of nextHeights.keys()) {
          if (!live.has(key)) nextHeights.delete(key);
        }
      }
      heightsRef.current = nextHeights;
      if (isFollowingRef.current()) measureViewRef.current();
      setHeights(nextHeights);
    });
    rowObserverRef.current = { observer, nodes: new Set() };
    return () => {
      observer.disconnect();
      rowObserverRef.current = null;
    };
  }, [contentRef, tracking, viewportRef]);

  useLayoutEffect(() => {
    const rows = rowObserverRef.current;
    const content = contentRef.current;
    if (!rows || !content) return;
    const mounted = new Set(content.querySelectorAll<HTMLElement>("[data-virtual-group], [data-virtual-row]"));
    for (const node of rows.nodes) {
      if (!mounted.has(node)) {
        rows.observer.unobserve(node);
        rows.nodes.delete(node);
      }
    }
    for (const node of mounted) {
      if (!rows.nodes.has(node)) {
        rows.observer.observe(node);
        rows.nodes.add(node);
      }
    }
  });

  return { range, rowRange, measureView };
}
