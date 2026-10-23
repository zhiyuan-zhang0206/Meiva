import { contextUnits } from "./timeline-model-context";
import { isSelected, type Selection } from "./timeline-model-selection";
import { type AxisMap } from "./timeline-model-axis";
import { type Viewport } from "./timeline-model-viewport";
import {
  INPUT_ROW,
  UNITS_ROW,
  levelRowId,
  selectionKey,
  type NavData,
  type NavItem,
  type NavRow,
} from "./timeline-nav-rows";

const rowCache = new WeakMap<NavData, WeakMap<object, Map<string, NavRow>>>();

export function navRow(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavRow {
  let byAxis = rowCache.get(data);
  if (byAxis === undefined) {
    byAxis = new WeakMap();
    rowCache.set(data, byAxis);
  }
  let rows = byAxis.get(axis);
  if (rows === undefined) {
    rows = new Map();
    byAxis.set(axis, rows);
  }
  let cached = rows.get(row);
  if (cached === undefined) {
    const items = buildItems(row, data, axis);
    cached = {
      items,
      keyed: items.map((item) => ({ key: selectionKey(item.selection), u0: item.u0, u1: item.u1 })),
      byKey: new Map(items.map((item) => [selectionKey(item.selection), item])),
    };
    rows.set(row, cached);
  }
  return cached;
}

export function navItems(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavItem[] {
  return navRow(row, data, axis).items;
}

function buildItems(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavItem[] {
  let items: NavItem[];
  if (row === UNITS_ROW) {
    items = data.units.map((unit) => ({
      row,
      unit,
      selection: { kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind },
      ...axis.unitSpan(unit),
      start: Date.parse(unit.start),
      end: Date.parse(unit.end),
    }));
  } else if (row === INPUT_ROW) {
    items = contextUnits(data.units).map((unit) => ({
      row,
      unit,
      selection: { kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind },
      ...axis.unitSpan(unit),
      start: Date.parse(unit.start),
      end: Date.parse(unit.end),
    }));
  } else {
    items = data.nodes
      .filter((node) => levelRowId(node.level) === row)
      .map((node) => ({
        row,
        node,
        selection: { kind: "node", id: node.id },
        ...axis.nodeSpan(node),
        start: Date.parse(node.start),
        end: Date.parse(node.end),
      }));
  }
  return items.sort((a, b) => a.u0 - b.u0 || a.u1 - b.u1 || a.start - b.start);
}

function indexOfSelection(items: readonly NavItem[], selection: Selection): number {
  return items.findIndex((item) => isSelected(selection, item.selection));
}

function isParentOf(parent: NavItem, child: NavItem): boolean {
  if (parent.node !== undefined) return child.node?.parent === parent.node.id || child.unit?.parent === parent.node.id;
  return parent.row === UNITS_ROW && child.row === INPUT_ROW && parent.unit === child.unit;
}

function overlapScore(here: NavItem, target: NavItem): number {
  return Math.min(here.u1, target.u1) - Math.max(here.u0, target.u0);
}

export function verticalTarget(here: NavItem, targets: readonly NavItem[], related = true): NavItem | undefined {
  const kin = related ? targets.find((item) => isParentOf(here, item) || isParentOf(item, here)) : undefined;
  if (kin !== undefined) return kin;
  return targets.reduce<NavItem | undefined>((best, item) => {
    if (best === undefined) return item;
    const gain = overlapScore(here, item) - overlapScore(here, best);
    if (gain !== 0) return gain > 0 ? item : best;
    // meiva:note
    // Equal: the one whose middle is nearer.
    const mid = (here.u0 + here.u1) / 2;
    return Math.abs((item.u0 + item.u1) / 2 - mid) < Math.abs((best.u0 + best.u1) / 2 - mid) ? item : best;
  }, undefined);
}

export function rowOfSelection(selection: Selection, data: NavData): string | null {
  if (selection.kind === "unit") return UNITS_ROW;
  const node = data.nodes.find((candidate) => candidate.id === selection.id);
  return node === undefined ? null : levelRowId(node.level);
}

export function firstInView(
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  view: Viewport,
  rows: readonly string[],
): { row: string; item: NavItem } | null {
  const order = [UNITS_ROW, ...rows.filter((row) => row !== UNITS_ROW).reverse()].filter((row) => rows.includes(row));
  for (const row of order) {
    const inView = navItems(row, data, axis).find((item) => item.end >= view.from && item.start <= view.to);
    if (inView !== undefined) return { row, item: inView };
  }
  return null;
}

export function locate(
  current: { row: string | null; selection: Selection },
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  rows: readonly string[],
): { row: string; items: NavItem[]; at: number } | null {
  let row = current.row !== null && rows.includes(current.row) ? current.row : null;
  let items = row === null ? [] : navItems(row, data, axis);
  let at = row === null ? -1 : indexOfSelection(items, current.selection);
  if (at < 0) {
    row = rowOfSelection(current.selection, data);
    items = row === null || !rows.includes(row) ? [] : navItems(row, data, axis);
    at = indexOfSelection(items, current.selection);
  }
  return row === null || at < 0 ? null : { row, items, at };
}
