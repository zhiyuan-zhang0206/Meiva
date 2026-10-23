// Keyboard navigation and the selection overlay of the run timeline: pure functions over the rows'
// items on the shared x axis. No React, no I/O.

import { chainIds, type Selection } from "./timeline-model-selection";
import { type AxisMap } from "./timeline-model-axis";
import { type Viewport } from "./timeline-model-viewport";
import {
  INPUT_ROW,
  UNITS_ROW,
  levelRowId,
  navRowIds,
  selectionKey,
  type NavData,
  type NavItem,
  type NavKey,
} from "./timeline-nav-rows";
import { firstInView, locate, navItems, rowOfSelection, verticalTarget } from "./timeline-nav-items";

export function navigate(
  key: NavKey,
  current: { row: string | null; selection: Selection } | null,
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  view: Viewport,
  rows: readonly string[] = navRowIds(data),
): { row: string; item: NavItem } | null {
  if (current === null) return firstInView(data, axis, view, rows);
  const here = locate(current, data, axis, rows);
  if (here === null) return firstInView(data, axis, view, rows);
  const { row, items, at } = here;
  if (key === "left" || key === "right") {
    const to = at + (key === "left" ? -1 : 1);
    const next = to < 0 ? undefined : items.at(to);
    return next === undefined ? null : { row, item: next };
  }
  const to = rows.indexOf(row) + (key === "up" ? -1 : 1);
  const targetRow = to < 0 ? undefined : rows.at(to);
  if (targetRow === undefined) return null;
  const found = verticalTarget(items[at], navItems(targetRow, data, axis));
  return found === undefined ? null : { row: targetRow, item: found };
}

/** A primary item drawn narrower than this also gets a faint line through every row, so it can be found. */
export const SELECTION_LINE_BELOW_PX = 6;

/** What the selection lights: the one primary item (the row the cursor is in), and the items linked to it, per row. */
export interface SelectionRoles {
  primary: { row: string; key: string } | null;
  linked: ReadonlyMap<string, ReadonlySet<string>>;
}

export function selectionRoles(
  current: { row: string | null; selection: Selection } | null,
  data: NavData,
  rows: readonly string[] = navRowIds(data),
): SelectionRoles {
  const linked = new Map<string, Set<string>>();
  if (current === null) return { primary: null, linked };
  const { selection } = current;
  // Only a block can sit in either of two rows (Messages, Context size); a node is in the row of its level.
  const row =
    selection.kind === "unit" && current.row === INPUT_ROW && rows.includes(INPUT_ROW)
      ? INPUT_ROW
      : rowOfSelection(selection, data);
  const add = (target: string, key: string) => {
    let keys = linked.get(target);
    if (keys === undefined) {
      keys = new Set();
      linked.set(target, keys);
    }
    keys.add(key);
  };
  const addAncestors = (ids: ReadonlySet<string>) => {
    for (const id of ids) {
      const node = data.nodes.find((candidate) => candidate.id === id);
      if (node !== undefined) add(levelRowId(node.level), `n${id}`);
    }
  };
  const primaryKey = selectionKey(selection);
  addAncestors(chainIds(selection, data.nodes, data.units));
  if (selection.kind === "unit") {
    // The block and its bar are one thing in two rows: whichever the cursor is in, the other is linked.
    const drawn = data.units.some((unit) => unit.context_total !== null && selectionKey({ kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind }) === primaryKey);
    if (drawn) add(row === INPUT_ROW ? UNITS_ROW : INPUT_ROW, primaryKey);
  }
  if (row === null) return { primary: null, linked };
  const own = linked.get(row);
  own?.delete(primaryKey);
  if (own?.size === 0) linked.delete(row);
  return { primary: { row, key: primaryKey }, linked };
}

/** The viewport that shows [startMs, endMs] when `view` does not: the same width, centred on it, inside the base extent; `view` itself when it already shows some of it. */
export function revealView(axis: AxisMap, view: Viewport, base: Viewport, startMs: number, endMs: number): Viewport {
  const u0 = axis.toU(startMs);
  const u1 = axis.toU(endMs);
  const shown = axis.viewU(view);
  if (u1 >= shown.from && u0 <= shown.to) return view;
  const width = shown.to - shown.from;
  const baseU = axis.viewU(base);
  const from = Math.min(Math.max((u0 + u1) / 2 - width / 2, baseU.from), baseU.to - width);
  return axis.viewFromU({ from, to: from + width });
}
