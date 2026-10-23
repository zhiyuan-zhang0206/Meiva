import type { RunTimelineUnit } from "@/lib/contracts/types";
import { categoryColor } from "@/lib/context-colors";
import { EXTERNAL_AGENT_PREFIX } from "@/lib/format/inbound-source";

// meiva:note
// The legend kinds, in drawing order: a block's kind, inbound split by sender.
export type BlockClass =
  | "human"
  | "agent"
  | "text"
  | "thinking"
  | "call"
  | "output"
  | "note";
export const BLOCK_CLASSES: readonly BlockClass[] = [
  "human",
  "agent",
  "text",
  "thinking",
  "call",
  "output",
  "note",
];

export function blockClass(unit: Pick<RunTimelineUnit, "kind" | "source">): BlockClass {
  if (unit.kind === "inbound") {
    return unit.source?.startsWith("agent:") || unit.source?.startsWith(EXTERNAL_AGENT_PREFIX) ? "agent" : "human";
  }
  return unit.kind;
}

const CLASS_CATEGORY: Record<BlockClass, string> = {
  human: "user_input",
  agent: "agent_messages",
  text: "output",
  thinking: "reasoning",
  call: "tool_call",
  output: "tool_response",
  note: "system_notes",
};

// meiva:note
// The color of a kind of block: the context breakdown's color of the same kind of content.
export function classColor(kind: BlockClass): string {
  return categoryColor(CLASS_CATEGORY[kind]);
}

export function unitColor(unit: Pick<RunTimelineUnit, "kind" | "source">): string {
  return classColor(blockClass(unit));
}

// meiva:note
// The context breakdown category a block class is drawn from, and back (a category with no blocks maps to null).
export function classCategory(cls: BlockClass): string {
  return CLASS_CATEGORY[cls];
}

export function categoryClass(category: string): BlockClass | null {
  return BLOCK_CLASSES.find((cls) => CLASS_CATEGORY[cls] === category) ?? null;
}
