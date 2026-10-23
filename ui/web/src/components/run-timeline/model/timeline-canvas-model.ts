// Pure model of the canvas rows: where each item of a row is drawn in pixels, how items narrower
// than a few pixels collapse to one drawn column each, and which item a pointer position hits.
// No React, no canvas, no I/O.

import { layoutSpans, visibleRun, type AxisMap } from "./timeline-model-axis";
import { maxBlockTokens, maxContextTotal } from "./timeline-model-context";
import { type Viewport } from "./timeline-model-viewport";
import { INPUT_ROW, UNITS_ROW, navRowIds, type NavData, type NavItem, type NavRow } from "./timeline-nav-rows";
import { navRow } from "./timeline-nav-items";
import { aggregateColumns, type Place, type RowLayout } from "./timeline-canvas-hit";

type Placer = Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">;

function layoutOf(
  places: readonly Place[],
  row: NavRow,
): RowLayout {
  const { wide, cells } = aggregateColumns(places);
  const drawn = new Set<string>();
  for (const place of wide) drawn.add(place.key);
  for (const cell of cells) drawn.add(cell.key);
  const boxes = new Map<string, { x0: number; x1: number }>();
  const items = new Map<string, NavItem>();
  const values = new Map<string, number>();
  for (const place of places) {
    if (!drawn.has(place.key)) continue;
    boxes.set(place.key, { x0: place.x0, x1: place.x1 });
    values.set(place.key, place.weight);
    const item = row.byKey.get(place.key);
    if (item !== undefined) items.set(place.key, item);
  }
  return { wide, cells, items, boxes, values };
}

/** The layout of a row: items placed on the axis (see `layoutSpans`), the weight of each deciding which of several narrow ones stands for a pixel column. */
function spanLayout(
  row: NavRow,
  viewU: Viewport,
  trackPx: number,
  barOf?: (item: NavItem) => number,
): RowLayout {
  const run = visibleRun(row.keyed, viewU);
  const keyed = run.start === 0 && run.end === row.keyed.length ? row.keyed : row.keyed.slice(run.start, run.end);
  const weightOf = (key: string): number => {
    if (barOf === undefined) return 0;
    const item = row.byKey.get(key);
    return item === undefined ? 0 : barOf(item);
  };
  const places: Place[] = layoutSpans(keyed, viewU, trackPx).map((place) => ({
    key: place.key,
    x0: place.left,
    x1: place.left + place.width,
    weight: weightOf(place.key),
  }));
  return layoutOf(places, row);
}

/** What a block's height is in the rows that draw one: its tokens by square root (Messages), or the context through it (Context size). */
export function barValue(row: string, unit: { context_tokens: number | null; context_total: number | null }): number {
  return row === UNITS_ROW ? Math.sqrt(unit.context_tokens ?? 0) : (unit.context_total ?? 0);
}

/** The value the row's tallest bar stands for. */
export function barTop(row: string, data: NavData): number {
  return row === UNITS_ROW ? Math.sqrt(maxBlockTokens(data.units)) : maxContextTotal(data.units);
}

/**
 * The layout of any row of the page by its id. The Messages and Context size rows hold the same blocks
 * (the second only those a request has read) at the same x and width; each block's value decides which
 * of several narrow ones stands for a pixel column.
 */
export function rowLayout(row: string, data: NavData, axis: Placer, viewU: Viewport, trackPx: number): RowLayout {
  const cached = navRow(row, data, axis);
  if (row !== UNITS_ROW && row !== INPUT_ROW) return spanLayout(cached, viewU, trackPx);
  return spanLayout(cached, viewU, trackPx, (item) => (item.unit === undefined ? 0 : barValue(row, item.unit)));
}

interface CachedLayouts {
  axis: Placer;
  from: number;
  to: number;
  trackPx: number;
  rows: string;
  layouts: Map<string, RowLayout>;
}

const cache = new WeakMap<NavData, CachedLayouts>();

function shiftLayout(layout: RowLayout, dx: number): RowLayout {
  return {
    wide: layout.wide.map((place) => ({ ...place, x0: place.x0 - dx, x1: place.x1 - dx })),
    cells: layout.cells.map((cell) => ({ ...cell, x0: cell.x0 - dx, x1: cell.x1 - dx })),
    items: layout.items,
    boxes: new Map([...layout.boxes].map(([key, box]) => [key, { x0: box.x0 - dx, x1: box.x1 - dx }])),
    values: layout.values,
  };
}

export function layoutsFor(
  data: NavData,
  axis: Placer,
  view: Viewport,
  trackPx: number,
  rows: readonly string[] = navRowIds(data),
): Map<string, RowLayout> {
  const hit = cache.get(data);
  const rowsKey = rows.join("|");
  if (hit?.axis === axis && hit.trackPx === trackPx && hit.rows === rowsKey) {
    if (hit.from === view.from && hit.to === view.to) return hit.layouts;
    const span = hit.to - hit.from;
    if (span > 0 && span === view.to - view.from) {
      const dx = ((view.from - hit.from) / span) * trackPx;
      const shifted = new Map([...hit.layouts].map(([row, layout]) => [row, shiftLayout(layout, dx)]));
      cache.set(data, { axis, from: view.from, to: view.to, trackPx, rows: rowsKey, layouts: shifted });
      return shifted;
    }
  }
  const layouts = new Map(rows.map((row) => [row, rowLayout(row, data, axis, view, trackPx)]));
  cache.set(data, { axis, from: view.from, to: view.to, trackPx, rows: rowsKey, layouts });
  return layouts;
}
