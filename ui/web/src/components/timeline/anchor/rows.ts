import type { BackendTimelineItem } from "@/lib/contracts/types";
import { canonicalBufferCoordinate, type CompactCoordinate, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { parseItemIdParts } from "@/lib/timeline/timeline";

import type { RowRef } from "./dom";

// meiva:note
// A saved row outlives its id: a compact re-keys rows and a reload reads them from another window.
// The identity a row carries is its id and rank, then its source `reading_locator`, then its content
// (kind and text, which a re-key keeps; ties go to the nearest canonical coordinate), then the
// coordinate alone. An id is never matched across ranks: a compact restarts message numbers, so a
// new row can reuse the id of the one the reader was on.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export interface RowIdentity {
  row: RowRef;
  locator?: string;
  coordinate?: CompactCoordinate;
  content?: { kind: string; payload: string };
}

interface Candidate {
  item: BackendTimelineItem;
  rank: number;
  coordinate: CompactCoordinate | null;
}

function canonicalCandidate(item: BackendTimelineItem): Candidate {
  const parts = parseItemIdParts(item.item_id);
  return { item, rank: parts?.rank ?? 0, coordinate: parts ? { rank: parts.rank, msg: parts.msg, block: parts.block } : null };
}

function candidates(canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): Candidate[] {
  const rows: Candidate[] = (buffer?.rows ?? []).map((row) => ({ item: row.item, rank: row.rank, coordinate: canonicalBufferCoordinate(row) }));
  for (const item of canonical) rows.push(canonicalCandidate(item));
  return rows;
}

const real = (candidate: Candidate) => !candidate.item.item_id.startsWith("_");

// meiva:note
// The loaded row a ref names, in the order `candidates` lists them. Identify runs on every reader
// scroll that moves the anchor, so it compares ids and parses only the rows that match.
function find(row: RowRef, canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): Candidate | undefined {
  for (const held of buffer?.rows ?? []) {
    if (held.item.item_id === row.itemId && held.rank === row.rank) return { item: held.item, rank: held.rank, coordinate: canonicalBufferCoordinate(held) };
  }
  for (const item of canonical) {
    if (item.item_id !== row.itemId) continue;
    const candidate = canonicalCandidate(item);
    if (candidate.rank === row.rank) return candidate;
  }
  return undefined;
}

export function identify(row: RowRef, canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): RowIdentity {
  const hit = find(row, canonical, buffer);
  const locator = hit?.item.reading_locator;
  return {
    row,
    ...(typeof locator === "string" && locator.length > 0 ? { locator } : {}),
    ...(hit?.coordinate ? { coordinate: hit.coordinate } : {}),
    ...(hit ? { content: { kind: hit.item.kind, payload: hit.item.payload } } : {}),
  };
}

// meiva:note
// The row this identity names in the loaded rows, or null when they do not hold it.
export function locate(identity: RowIdentity, canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): RowRef | null {
  const rows = candidates(canonical, buffer).filter(real);
  const as = (candidate: Candidate): RowRef => ({ itemId: candidate.item.item_id, rank: candidate.rank, ...(identity.row.turn ? { turn: true as const } : {}) });
  const exact = rows.find((candidate) => candidate.item.item_id === identity.row.itemId && candidate.rank === identity.row.rank);
  if (exact) return identity.row;
  if (identity.locator) {
    const byLocator = rows.find((candidate) => candidate.item.reading_locator === identity.locator);
    if (byLocator) return as(byLocator);
  }
  const coordinate = identity.coordinate;
  const distance = (candidate: Candidate) => coordinate && candidate.coordinate
    ? Math.abs(candidate.coordinate.rank - coordinate.rank) * 1e6 + Math.abs(candidate.coordinate.msg - coordinate.msg) * 1e3 + Math.abs(candidate.coordinate.block - coordinate.block)
    : 0;
  const content = identity.content;
  if (content && content.payload.length > 0) {
    const same = rows.filter((candidate) => candidate.item.kind === content.kind && candidate.item.payload === content.payload);
    // meiva:note
    // A repeated text ("continue") names its row only with a coordinate to choose by.
    if (same.length === 1) return as(same[0]);
    if (same.length > 1 && coordinate) return as(same.reduce((best, candidate) => (distance(candidate) < distance(best) ? candidate : best)));
  }
  if (coordinate) {
    const byCoordinate = rows.find((candidate) => candidate.coordinate !== null &&
      candidate.coordinate.rank === coordinate.rank && candidate.coordinate.msg === coordinate.msg && candidate.coordinate.block === coordinate.block);
    if (byCoordinate) return as(byCoordinate);
  }
  return null;
}

// meiva:note
// A compact moves the reader's rows into the buffer one rank older; the coordinate is where the
// canonical rows that replace them will be found.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
export function shiftForCompact(identity: RowIdentity, rankShift: number): RowIdentity {
  if (rankShift <= 0) return identity;
  const coordinate = identity.coordinate;
  return {
    ...identity,
    row: { ...identity.row, rank: identity.row.rank + rankShift },
    ...(coordinate ? { coordinate: { rank: coordinate.rank + rankShift, msg: coordinate.msg, block: coordinate.block } } : {}),
  };
}

export function rankOf(item: BackendTimelineItem, buffer: CompactTransitionBuffer | null): number {
  return buffer?.rows.find((row) => row.item === item)?.rank ?? parseItemIdParts(item.item_id)?.rank ?? 0;
}
