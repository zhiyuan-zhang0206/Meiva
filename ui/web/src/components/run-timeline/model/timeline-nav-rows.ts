import type { RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";

import { contextUnits } from "./timeline-model-context";
import { levelsTopFirst, type Selection } from "./timeline-model-selection";
import { type SpanItem } from "./timeline-model-axis";

// meiva:note
// What keyboard navigation moves over.
export interface NavData {
  nodes: readonly RunTimelineNode[];
  units: readonly RunTimelineUnit[];
}

export type NavKey = "left" | "right" | "up" | "down";

// meiva:note
// One selectable thing of a row: a node or a block (in the Messages row and, as a bar, in the Context size row). Every row is a list of
// these, each with its extent on the shared x axis (`u0`..`u1`, axis coordinates) and in time.
export interface NavItem {
  row: string;
  selection: Selection;
  u0: number;
  u1: number;
  // meiva:note
  // Its extent in epoch milliseconds.
  start: number;
  end: number;
  node?: RunTimelineNode;
  unit?: RunTimelineUnit;
}

export const UNITS_ROW = "units";
export const INPUT_ROW = "input";
export const levelRowId = (level: number) => `level-${level}`;

// meiva:note
// What the page's settings keep of an agent's rows.
export interface RowOptions {
  // meiva:note
  // How many understanding-tree levels are drawn, counted from the topmost; null draws them all.
  levels: number | null;
  // meiva:note
  // Whether the Context size row is drawn.
  contextSize: boolean;
}

export const ALL_ROWS: RowOptions = { levels: null, contextSize: true };

// meiva:note
// The rows top to bottom, as the page draws them.
export function navRowIds(data: NavData, options: RowOptions = ALL_ROWS): string[] {
  const levels = levelsTopFirst(data.nodes);
  const rows = (options.levels === null ? levels : levels.slice(0, options.levels)).map(levelRowId);
  rows.push(UNITS_ROW);
  if (options.contextSize && contextUnits(data.units).length > 0) rows.push(INPUT_ROW);
  return rows;
}

// meiva:note
// A row's items are built once per data and axis, together with the two lookups the layout and the hit test ask for on every frame: the same items as spans in `u0` order, and the key-to-item map. Rebuilding them per render made a deep zoom-out recreate 52 000 keys and a 52 000-entry map on every viewport sample (user report 2026-10-19).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
export interface NavRow {
  items: NavItem[];
  keyed: readonly SpanItem[];
  byKey: ReadonlyMap<string, NavItem>;
}

// meiva:note
// The identity of an item across redraws.
export function selectionKey(selection: Selection): string {
  if (selection.kind === "node") return `n${selection.id}`;
  return `u${selection.unitKind}-${selection.i0}-${selection.i1}`;
}
