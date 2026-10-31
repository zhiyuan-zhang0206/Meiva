import { NARROW_DRAW_PX } from "./timeline-model-axis";
import type { NavItem } from "./timeline-nav-rows";

// meiva:note
// One item of a row on the track, in pixels from its left edge.
export interface Place {
  key: string;
  x0: number;
  x1: number;
  // meiva:note
  // What decides which of several narrow items stands for their pixel column (the larger wins; a tie goes to the one covering more of the column).
  weight: number;
}

// meiva:note
// A run of whole pixel columns painted for one item: the item that stands for each of them.
export interface Cell {
  x0: number;
  x1: number;
  key: string;
}

export interface RowLayout {
  // meiva:note
  // Items wide enough to be drawn as blocks, in x order.
  wide: Place[];
  // meiva:note
  // The narrow items, collapsed to one painted item per pixel column and run-merged.
  cells: Cell[];
  items: ReadonlyMap<string, NavItem>;
  // meiva:note
  // Where each item of the row is drawn in pixels, before narrow ones collapse to columns: what a frame hugs.
  boxes: ReadonlyMap<string, { x0: number; x1: number }>;
  // meiva:note
  // A bar row's value per item (what its height is), absent for other rows.
  values?: ReadonlyMap<string, number>;
}

// meiva:note
// Splits places into blocks (at least `narrowPx` wide) and one painted item per pixel column for
// the rest: of the narrow items touching a column, the heaviest wins, then the one covering most
// of it. Every column is painted at most once, so piling up items never darkens or thickens it.
export function aggregateColumns(
  places: readonly Place[],
  narrowPx: number = NARROW_DRAW_PX,
): { wide: Place[]; cells: Cell[] } {
  const wide: Place[] = [];
  const best = new Map<number, { key: string; weight: number; cover: number }>();
  for (const place of places) {
    if (place.x1 - place.x0 >= narrowPx) {
      wide.push(place);
      continue;
    }
    const first = Math.floor(place.x0);
    const last = Math.max(Math.ceil(place.x1), first + 1);
    for (let column = first; column < last; column += 1) {
      const cover = Math.max(Math.min(place.x1, column + 1) - Math.max(place.x0, column), 1e-6);
      const held = best.get(column);
      if (held === undefined || place.weight > held.weight || (place.weight === held.weight && cover > held.cover)) {
        best.set(column, { key: place.key, weight: place.weight, cover });
      }
    }
  }
  const cells: Cell[] = [];
  for (const column of [...best.keys()].sort((a, b) => a - b)) {
    // meiva:note
    // Every column here came from `best`, so the reader states that instead of asserting it: a column
    // that somehow has no entry draws nothing.
    // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
    const held = best.get(column);
    if (held === undefined) continue;
    const { key } = held;
    const tail = cells.at(-1);
    if (tail?.x1 === column && tail.key === key) tail.x1 = column + 1;
    else cells.push({ x0: column, x1: column + 1, key });
  }
  return { wide: wide.sort((a, b) => a.x0 - b.x0 || a.x1 - b.x1), cells };
}

// meiva:note
// Largest end among `wide[0..i]`, so a search can stop looking back.
function runningEnds(wide: readonly Place[]): number[] {
  const ends: number[] = [];
  for (const place of wide) ends.push(Math.max(place.x1, ends.at(-1) ?? -Infinity));
  return ends;
}

// meiva:note
// A row's layout made searchable: items sorted by x, with the running maximum of their ends.
export interface HitIndex {
  wide: readonly Place[];
  ends: readonly number[];
  cells: readonly Cell[];
}

export function buildHitIndex(layout: Pick<RowLayout, "wide" | "cells">): HitIndex {
  return { wide: layout.wide, ends: runningEnds(layout.wide), cells: layout.cells };
}

// meiva:note
// The last index in `sorted` whose `start` is at most `x`, or -1.
function lastAtOrBefore(count: number, startAt: (i: number) => number, x: number): number {
  let lo = 0;
  let hi = count - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (startAt(mid) <= x) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

// meiva:note
// The key of the item under pixel `x` of a row, or null. A painted column (a narrow item) wins over
// the block under it, since it is drawn on top; of overlapping blocks the later-starting one wins; a
// column also answers for the `tolerance` pixels beside it, so a hairline stays reachable.
export function hitTest(index: HitIndex, x: number, tolerance = 1): string | null {
  // meiva:note
  // The hairlines are drawn over the blocks, so they answer first.
  const at = lastAtOrBefore(index.cells.length, (k) => index.cells[k].x0, x);
  const here = at >= 0 ? index.cells[at] : undefined;
  if (here !== undefined && x < here.x1) return here.key;
  for (let i = lastAtOrBefore(index.wide.length, (k) => index.wide[k].x0, x); i >= 0 && index.ends[i] >= x; i -= 1) {
    if (index.wide[i].x1 >= x) return index.wide[i].key;
  }
  const near = [here, index.cells[at + 1]]
    .filter((cell): cell is Cell => cell !== undefined)
    .map((cell) => ({ cell, gap: x < cell.x0 ? cell.x0 - x : x - cell.x1 }))
    .filter(({ gap }) => gap <= tolerance)
    .sort((a, b) => a.gap - b.gap)
    .at(0);
  return near === undefined ? null : near.cell.key;
}

// meiva:note
// Snaps a CSS-pixel coordinate to the device pixel grid, so a fill has no half-covered edge.
export function snap(x: number, dpr: number): number {
  return Math.round(x * dpr) / dpr;
}

// meiva:note
// The frame around a set of items of a row: the union of the parts of their boxes that lie on the
// track (an item running past an edge is framed only where it is visible). Null when none of them is
// on the track.
export function frameOf(
  boxes: readonly { x0: number; x1: number }[],
  trackPx: number,
): { left: number; width: number } | null {
  let x0 = Infinity;
  let x1 = -Infinity;
  for (const box of boxes) {
    if (box.x1 < 0 || box.x0 > trackPx) continue;
    x0 = Math.min(x0, Math.max(box.x0, 0));
    x1 = Math.max(x1, Math.min(box.x1, trackPx));
  }
  if (x0 > x1) return null;
  return { left: x0, width: x1 - x0 };
}
