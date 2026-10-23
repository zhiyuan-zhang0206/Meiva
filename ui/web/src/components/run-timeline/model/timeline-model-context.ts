import type { RunTimelineUnit } from "@/lib/contracts/types";

// meiva:note
// The blocks the Context size row draws: those a request has read.
export function contextUnits(units: readonly RunTimelineUnit[]): RunTimelineUnit[] {
  return units.filter((unit) => unit.context_total !== null);
}

// meiva:note
// The largest context total among the blocks: what the Context size row scales to.
export function maxContextTotal(units: readonly RunTimelineUnit[]): number {
  return units.reduce((top, unit) => Math.max(top, unit.context_total ?? 0), 0);
}

// meiva:note
// The largest block by tokens: what a Messages row that draws heights by tokens scales to.
export function maxBlockTokens(units: readonly RunTimelineUnit[]): number {
  return units.reduce((top, unit) => Math.max(top, unit.context_tokens ?? 0), 0);
}
