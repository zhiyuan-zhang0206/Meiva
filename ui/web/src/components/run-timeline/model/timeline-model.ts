// Pure model of the run-timeline page: where a span sits on the window's axis,
// which raw-message parts belong to a unit, and how the arrow keys move over the rows.
// No React, no I/O.

import { projectBox, type AxisMap, type AxisSpan } from "./timeline-model-axis";
import { MIN_VIEW_MS, panViewport, zoomViewport, type Viewport } from "./timeline-model-viewport";

/** The x map of the timeline over the loaded extent `base`: linear in time. */
export function timeAxis(base: Viewport): AxisMap {
  const total = Math.max(base.to - base.from, 1);
  const toU = (ms: number) => Math.min(Math.max(ms - base.from, 0), total);
  const fromU = (u: number) => base.from + Math.min(Math.max(u, 0), total);
  const span = (start: string, end: string): AxisSpan => ({ u0: toU(Date.parse(start)), u1: toU(Date.parse(end)) });
  return {
    total,
    minSpan: MIN_VIEW_MS,
    toU,
    fromU,
    viewU: (view) => ({ from: toU(view.from), to: toU(view.to) }),
    viewFromU: (view) => {
      const from = fromU(view.from);
      return { from, to: Math.max(fromU(view.to), from + 1) };
    },
    unitSpan: (unit) => span(unit.start, unit.end),
    nodeSpan: (node) => span(node.start, node.end),
  };
}

/** Zooms a time viewport by `factor` around the point `frac` across the view, in the axis's own coordinates. */
export function zoomView(axis: AxisMap, view: Viewport, base: Viewport, frac: number, factor: number): Viewport {
  return axis.viewFromU(zoomViewport(axis.viewU(view), axis.viewU(base), frac, factor, axis.minSpan));
}

/** Pans a time viewport by `fraction` of its width on the axis (positive = later). */
export function panView(axis: AxisMap, view: Viewport, base: Viewport, fraction: number): Viewport {
  return axis.viewFromU(panViewport(axis.viewU(view), axis.viewU(base), fraction));
}

/** A time span's box on the current view of an axis, as percentages; null when it lies outside. */
export function axisBox(
  axis: AxisMap,
  start: string,
  end: string,
  viewU: Viewport,
): { left: number; width: number } | null {
  return projectBox(axis.toU(Date.parse(start)), axis.toU(Date.parse(end)), viewU);
}
