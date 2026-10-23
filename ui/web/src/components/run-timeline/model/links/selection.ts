// meiva:note
// Pure model of the arrows between agents: which end of an agent-to-agent event lands where, the
// curve between two points and which curve a pointer is on. No React, no canvas, no I/O.

import type { RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { distanceToCurve, type Curve } from "./geometry";
import type { Selection } from "../timeline-model-selection";

const middle = (unit: Pick<RunTimelineUnit, "start" | "end">) => (Date.parse(unit.start) + Date.parse(unit.end)) / 2;

// meiva:note
// The key of the curve nearest to the pointer within `tolerance` pixels (the later-drawn one on a tie), or null.
export function hitLink(curves: readonly Curve[], px: number, py: number, tolerance: number): string | null {
  let key: string | null = null;
  let nearest = tolerance;
  for (const c of curves) {
    const d = distanceToCurve(c, px, py);
    if (d <= nearest) {
      nearest = d;
      key = c.key;
    }
  }
  return key;
}

// meiva:note
// When a selection of an agent starts, in epoch milliseconds (null when it is not in the data).
export function selectionMs(data: RunTimelineResponse, selection: Selection): number | null {
  if (selection.kind === "node") {
    const node = data.nodes.find((candidate) => candidate.id === selection.id);
    return node === undefined ? null : Date.parse(node.start);
  }
  const unit = data.units.find((u) => u.i0 === selection.i0 && u.i1 === selection.i1 && u.kind === selection.unitKind);
  return unit === undefined ? null : Date.parse(unit.start);
}

// meiva:note
// The block of the Messages row nearest in time to `ms`, as a selection.
export function nearestUnit(data: RunTimelineResponse, ms: number): Selection | null {
  let best: RunTimelineUnit | null = null;
  let gap = Infinity;
  for (const unit of data.units) {
    const d = Math.abs(middle(unit) - ms);
    if (d < gap) {
      gap = d;
      best = unit;
    }
  }
  return best === null ? null : { kind: "unit", i0: best.i0, i1: best.i1, unitKind: best.kind };
}
