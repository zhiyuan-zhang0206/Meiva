import type { BackendTimelineItem } from "@/lib/contracts/types";
import { canonicalBufferCoordinate, type CompactCoordinate, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { parseItemIdParts } from "@/lib/timeline/timeline";

import type { RowRef } from "./dom";

// meiva:note
// A saved row outlives its id: a compact re-keys rows and a reload reads them from another window.
// The identity a row carries is its id and rank, then its source `reading_locator`, then its
// canonical coordinate (rank, message, block) as a compact maps it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export interface RowIdentity {
  row: RowRef;
  locator?: string;
  coordinate?: CompactCoordinate;
}

interface Candidate {
  item: BackendTimelineItem;
  rank: number;
  coordinate: CompactCoordinate | null;
}

function candidates(canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): Candidate[] {
  const rows: Candidate[] = (buffer?.rows ?? []).map((row) => ({ item: row.item, rank: row.rank, coordinate: canonicalBufferCoordinate(row) }));
  for (const item of canonical) {
    const parts = parseItemIdParts(item.item_id);
    rows.push({ item, rank: parts?.rank ?? 0, coordinate: parts ? { rank: parts.rank, msg: parts.msg, block: parts.block } : null });
  }
  return rows;
}

const real = (candidate: Candidate) => !candidate.item.item_id.startsWith("_");

export function identify(row: RowRef, canonical: readonly BackendTimelineItem[], buffer: CompactTransitionBuffer | null): RowIdentity {
  const hit = candidates(canonical, buffer).find((candidate) => candidate.item.item_id === row.itemId && candidate.rank === row.rank);
  const locator = hit?.item.reading_locator;
  return {
    row,
    ...(typeof locator === "string" && locator.length > 0 ? { locator } : {}),
    ...(hit?.coordinate ? { coordinate: hit.coordinate } : {}),
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
  const sameId = rows.filter((candidate) => candidate.item.item_id === identity.row.itemId);
  if (sameId.length === 1) return as(sameId[0]);
  const coordinate = identity.coordinate;
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
  const coordinate = identity.coordinate;
  if (!coordinate || rankShift <= 0) return identity;
  return {
    ...identity,
    coordinate: {
      rank: coordinate.rank + rankShift,
      msg: coordinate.rank === 0 ? coordinate.msg - 1 : coordinate.msg,
      block: coordinate.block,
    },
  };
}

export function rankOf(item: BackendTimelineItem, buffer: CompactTransitionBuffer | null): number {
  return buffer?.rows.find((row) => row.item === item)?.rank ?? parseItemIdParts(item.item_id)?.rank ?? 0;
}
