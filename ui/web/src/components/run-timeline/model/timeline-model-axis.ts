import type { RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";

import type { TimelineWindow } from "./timeline-model-selection";
import { viewportOf, type Viewport } from "./timeline-model-viewport";

// meiva:note
// A span's box on an axis-coordinate view as percentages, clamped to the view; null when it lies outside.
export function projectBox(
  u0: number,
  u1: number,
  view: Viewport,
): { left: number; width: number } | null {
  if (!(view.to > view.from) || Number.isNaN(u0) || Number.isNaN(u1) || u1 < view.from || u0 > view.to) return null;
  const span = view.to - view.from;
  const left = (Math.max(u0, view.from) - view.from) / span;
  const right = (Math.min(u1, view.to) - view.from) / span;
  return { left: left * 100, width: Math.max(0, right - left) * 100 };
}

// meiva:note
// A span's box on the window axis as percentages, clamped to the window; null when it lies outside.
export function spanBox(
  start: string,
  end: string,
  window: TimelineWindow,
): { left: number; width: number } | null {
  return projectBox(Date.parse(start), Date.parse(end), viewportOf(window));
}

// meiva:note
// No item is drawn narrower than this, in pixels: an instant (a call point, an inbound message) is as wide as this.
export const MIN_ITEM_PX = 3;

// meiva:note
// An item drawn narrower than this gets no border or rounding: it is a fill, one per pixel column.
export const NARROW_DRAW_PX = 4;

// meiva:note
// Where one item of a row is drawn, in pixels from the track's left edge.
export interface RowPlacement {
  key: string;
  left: number;
  width: number;
}

// meiva:note
// One item of a row as a span of axis coordinates.
export interface SpanItem {
  key: string;
  u0: number;
  u1: number;
}

// meiva:note
// Only the run of items that can intersect the view is projected (`visibleRun`): the rows arrive sorted by `u0`, so a binary search finds the first candidate and the walk back covers an item that began before the view but still covers it. A pan therefore costs what is on screen, not what is loaded behind it: laying out a 52 000-unit deep zoom-out took ~190 ms per viewport sample (user report 2026-10-19). The placements also need no sort, because the axis is linear and the rows are already in `u0` order.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
export function layoutSpans(
  items: readonly SpanItem[],
  view: Viewport,
  trackPx: number,
  minPx: number = MIN_ITEM_PX,
): RowPlacement[] {
  const placements: RowPlacement[] = [];
  const { start, end } = visibleRun(items, view);
  const span = view.to - view.from;
  if (!(span > 0)) return placements;
  let ordered = true;
  let lastLeft = -Infinity;
  for (let i = start; i < end; i += 1) {
    const item = items[i];
    if (Number.isNaN(item.u0) || Number.isNaN(item.u1) || item.u1 < view.from || item.u0 > view.to) continue;
    const left = ((Math.max(item.u0, view.from) - view.from) / span) * trackPx;
    const right = ((Math.min(item.u1, view.to) - view.from) / span) * trackPx;
    const width = Math.min(Math.max(right - left, minPx), trackPx - left);
    if (left < lastLeft) ordered = false;
    lastLeft = left;
    placements.push({ key: item.key, left, width });
  }
  return ordered ? placements : placements.sort((a, b) => a.left - b.left || a.width - b.width);
}

export function visibleRun(items: readonly { u0: number; u1: number }[], view: Viewport): { start: number; end: number } {
  let start = firstStartingAtOrAfter(items, view.from);
  while (start > 0 && items[start - 1].u1 >= view.from) start -= 1;
  let end = start;
  while (end < items.length && items[end].u0 <= view.to) end += 1;
  return { start, end };
}

function firstStartingAtOrAfter(items: readonly { u0: number }[], u: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].u0 < u) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// meiva:note
// Where one block sits in axis coordinates.
export interface AxisSpan {
  u0: number;
  u1: number;
}

// meiva:note
// The map from time to the x coordinate every row of every agent shares. Axis coordinates `u` are
// milliseconds since the base start; rows only ever compare them against `viewU` of the current viewport.
export interface AxisMap {
  // meiva:note
  // Length of the whole base extent in axis coordinates.
  total: number;
  // meiva:note
  // The narrowest viewport a zoom may reach, in axis coordinates.
  minSpan: number;
  // meiva:note
  // Time to axis coordinate, clamped to the base extent.
  toU: (ms: number) => number;
  fromU: (u: number) => number;
  // meiva:note
  // The viewport in axis coordinates.
  viewU: (view: Viewport) => Viewport;
  // meiva:note
  // A viewport in axis coordinates back to time (at least 1 ms wide).
  viewFromU: (view: Viewport) => Viewport;
  unitSpan: (unit: Pick<RunTimelineUnit, "start" | "end">) => AxisSpan;
  nodeSpan: (node: Pick<RunTimelineNode, "start" | "end">) => AxisSpan;
}
