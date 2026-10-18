import type { RunTimelineResponse } from "@/lib/contracts/types";

import { unitKey, viewportOf, type Viewport } from "../model/timeline-model";

export function overlaps(a: Viewport, b: Viewport): boolean {
  return a.from <= b.to && b.from <= a.to;
}

export function uncovered(view: Viewport, ranges: readonly Viewport[]): Viewport[] {
  const gaps: Viewport[] = [];
  let from = view.from;
  for (const range of [...ranges].sort((a, b) => a.from - b.from)) {
    if (range.to <= from || range.from >= view.to) continue;
    if (range.from > from) gaps.push({ from, to: range.from });
    from = Math.max(from, range.to);
  }
  if (from < view.to) gaps.push({ from, to: view.to });
  return gaps;
}

export function padded(view: Viewport, lifetime: Viewport, margin: number): Viewport {
  const width = view.to - view.from;
  return { from: Math.floor(Math.max(lifetime.from, view.from - width * margin)), to: Math.ceil(Math.min(lifetime.to, view.to + width * margin)) };
}

export function completeRanges(data: readonly RunTimelineResponse[]): Viewport[] {
  return data.filter((item) => item.units_omitted === 0).map((item) => viewportOf(item.window));
}

export function omittedIn(data: readonly RunTimelineResponse[], visible: Viewport): number {
  return uncovered(visible, completeRanges(data)).length === 0 ? 0 : Math.max(0,
    ...data.filter((item) => overlaps(viewportOf(item.window), visible)).map((item) => item.units_omitted));
}

// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
export function mergeWindows(data: readonly RunTimelineResponse[], view: Viewport | null): RunTimelineResponse | undefined {
  const first = data.at(0);
  if (first === undefined) return undefined;
  if (data.length === 1) return first;
  const ranges = data.map((item) => viewportOf(item.window));
  const visible = view ?? viewportOf(first.window);
  const omitted = omittedIn(data, visible);
  return {
    ...first,
    window: { from: new Date(Math.min(...ranges.map((range) => range.from))).toISOString(), to: new Date(Math.max(...ranges.map((range) => range.to))).toISOString() },
    nodes: [...new Map(data.flatMap((item) => item.nodes).map((node) => [node.id, node])).values()].sort((a, b) => a.span_start - b.span_start),
    units: [...new Map(data.flatMap((item) => item.units).map((unit) => [unitKey(unit), unit])).values()].sort((a, b) => a.i0 - b.i0 || a.start.localeCompare(b.start)),
    units_omitted: omitted,
  };
}
