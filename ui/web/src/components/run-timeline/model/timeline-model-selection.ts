import type {
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineUnit,
} from "@/lib/contracts/types";

export interface TimelineWindow {
  from: string;
  to: string;
}

export type Selection =
  | { kind: "node"; id: string }
  | { kind: "unit"; i0: number; i1: number; unitKind: RunTimelineUnit["kind"] };

export function isSelected(selection: Selection | null, candidate: Selection): boolean {
  if (selection?.kind === "node" && candidate.kind === "node") return selection.id === candidate.id;
  if (selection?.kind === "unit" && candidate.kind === "unit") {
    return (
      selection.i0 === candidate.i0 &&
      selection.i1 === candidate.i1 &&
      selection.unitKind === candidate.unitKind
    );
  }
  return false;
}

export type Hover = Selection;

export function unitKey(unit: Pick<RunTimelineUnit, "kind" | "i0" | "i1">): string {
  return `${unit.kind}-${unit.i0}-${unit.i1}`;
}

// meiva:note
// The ids of the nodes a selection lights up: the selected node and every ancestor above it, or,
// for a layer-0 block, the level-1 node covering it and every ancestor above that. The chain stops where a parent is not in `nodes` (outside the loaded window).
export function chainIds(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
): Set<string> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const starts: (string | null)[] = [];
  if (selection?.kind === "node") starts.push(selection.id);
  else if (selection?.kind === "unit") {
    starts.push(
      units.find(
        (unit) =>
          unit.i0 === selection.i0 && unit.i1 === selection.i1 && unit.kind === selection.unitKind,
      )?.parent ?? null,
    );
  }
  const chain = new Set<string>();
  for (let next = starts.shift(); next !== undefined; next = starts.shift()) {
    while (next !== null && !chain.has(next)) {
      const node = byId.get(next);
      if (node === undefined) break;
      chain.add(next);
      next = node.parent;
    }
  }
  return chain;
}

// meiva:note
// The distinct node levels, topmost first (level 1 = the leaves, drawn last).
export function levelsTopFirst(nodes: readonly RunTimelineNode[]): number[] {
  return [...new Set(nodes.map((node) => node.level))].sort((a, b) => b - a);
}

export function firstLine(text: string, max: number): string {
  const line = text.split("\n").find((candidate) => candidate.trim() !== "") ?? "";
  const trimmed = line.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

const PART_OF_KIND: Partial<Record<RunTimelineUnit["kind"], RunTimelineMessagePart["kind"]>> = {
  thinking: "think",
  text: "text",
  call: "call",
  output: "out",
};

export function partsForUnit(
  kind: RunTimelineUnit["kind"],
  parts: readonly RunTimelineMessagePart[],
): RunTimelineMessagePart[] {
  const wanted = PART_OF_KIND[kind];
  return wanted === undefined ? [...parts] : parts.filter((part) => part.kind === wanted);
}
