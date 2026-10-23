import type { RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";

import { blockClass, type BlockClass } from "./timeline-model-classes";
import { chainIds, unitKey, type Hover } from "./timeline-model-selection";

// meiva:note
// What the legend (or a context breakdown row) highlights: every block of one class, optionally of one source only.
export interface Highlight {
  cls: BlockClass;
  source: string | null;
}

export function matchesHighlight(unit: Pick<RunTimelineUnit, "kind" | "source">, highlight: Highlight): boolean {
  return blockClass(unit) === highlight.cls && (highlight.source === null || unit.source === highlight.source);
}

// meiva:note
// The distinct sources of the inbound blocks of one class, in order of first appearance.
export function inboundSources(units: readonly RunTimelineUnit[], cls: "human" | "agent"): string[] {
  const sources: string[] = [];
  for (const unit of units) {
    if (unit.source !== null && blockClass(unit) === cls && !sources.includes(unit.source)) sources.push(unit.source);
  }
  return sources;
}

// meiva:note
// What hovering lights up, more softly than a selection: the hovered block's ancestor chain; for a
// node also the blocks its message span covers.
export function hoverLit(
  hover: Hover | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
): { nodeIds: Set<string>; unitKeys: Set<string> } {
  if (hover === null) return { nodeIds: new Set(), unitKeys: new Set() };
  const nodeIds = chainIds(hover, nodes, units);
  const node = hover.kind === "node" ? nodes.find((candidate) => candidate.id === hover.id) : undefined;
  const covered =
    node === undefined ? [] : units.filter((unit) => unit.i0 >= node.span_start && unit.i0 <= node.span_end);
  return { nodeIds, unitKeys: new Set(covered.map(unitKey)) };
}

// meiva:note
// A node's ancestors, nearest first, as far as they are loaded.
export function nodeAncestors(node: RunTimelineNode, nodes: readonly RunTimelineNode[]): RunTimelineNode[] {
  const byId = new Map(nodes.map((candidate) => [candidate.id, candidate]));
  const out: RunTimelineNode[] = [];
  let next = node.parent === null ? undefined : byId.get(node.parent);
  while (next !== undefined && !out.includes(next)) {
    out.push(next);
    next = next.parent === null ? undefined : byId.get(next.parent);
  }
  return out;
}

export function nodeChildren(node: RunTimelineNode, nodes: readonly RunTimelineNode[]): RunTimelineNode[] {
  return nodes.filter((candidate) => candidate.parent === node.id).sort((a, b) => a.span_start - b.span_start);
}
