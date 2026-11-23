import {
  readReloadBookmark,
  removeReloadBookmark,
  saveReloadBookmark,
  type ReloadRestoreConfig,
} from "../model/reading-position";

import type { ViewAnchor } from "./dom";
import type { RowIdentity } from "./rows";

// meiva:note
// A saved place is the anchor itself, kept twice:
// - per history entry in memory, so switching between agents in one page returns exactly;
// - per agent in `sessionStorage`, so a reload in the same tab returns too.
// Leaving the tail keeps a delivery watermark in page memory. A reload from the tail
// keeps no bookmark.
// meiva:link ui/web/src/docs/frontend-state/scroll-memory.md
export interface SavedPlace {
  identity: RowIdentity;
  offset: number;
  // meiva:note
  // Whether the saved offset stood in reserve space a header toggle created.
  holdReserve: boolean;
}

export interface TailDeparture {
  last: RowIdentity | null;
  partialPayload?: string;
}

type MemoryPlace = SavedPlace | TailDeparture;

const memory = new Map<string, { contentKey: string; place: MemoryPlace }>();

export function readMemoryPlace(entryKey: string | undefined, contentKey: string | undefined): MemoryPlace | null {
  if (!entryKey || contentKey === undefined) return null;
  const saved = memory.get(entryKey);
  return saved?.contentKey === contentKey ? saved.place : null;
}

export function writeMemoryPlace(entryKey: string | undefined, contentKey: string | undefined, place: MemoryPlace): void {
  if (!entryKey || contentKey === undefined) return;
  memory.set(entryKey, { contentKey, place });
}

export function readSessionPlace(contentKey: string | undefined, config: ReloadRestoreConfig): SavedPlace | null {
  if (contentKey === undefined || config.state !== "enabled") return null;
  const bookmark = readReloadBookmark(config.session, contentKey, config.now());
  if (!bookmark) return null;
  return {
    identity: {
      row: { itemId: bookmark.itemId, rank: bookmark.rank, ...(bookmark.turn ? { turn: true as const } : {}) },
      ...(bookmark.readingLocator ? { locator: bookmark.readingLocator } : {}),
    },
    offset: bookmark.viewportTop,
    holdReserve: bookmark.usedCollapseReserve === true,
  };
}

export function writeSessionPlace(contentKey: string | undefined, config: ReloadRestoreConfig, place: SavedPlace | null, scrollTop: number): void {
  if (contentKey === undefined || config.state !== "enabled") return;
  const now = config.now();
  if (place === null) {
    removeReloadBookmark(config.session, contentKey, now);
    return;
  }
  const { row, locator } = place.identity;
  saveReloadBookmark(config.session, contentKey, {
    ...(locator ? { readingLocator: locator } : {}),
    itemId: row.itemId,
    rank: row.rank,
    ...(row.turn ? { turn: true as const } : {}),
    viewportTop: place.offset,
    scrollTop: Math.max(0, scrollTop),
    ...(place.holdReserve ? { usedCollapseReserve: true as const } : {}),
    savedAt: now,
  }, now);
}

// meiva:note
// Whether a conversation opens on a saved place rather than its tail or first unseen delivery.
export function hasSavedPlace(entryKey: string | undefined, contentKey: string | undefined, config: ReloadRestoreConfig): boolean {
  const memory = readMemoryPlace(entryKey, contentKey);
  return memory ? "identity" in memory : readSessionPlace(contentKey, config) !== null;
}

export function rowAnchor(place: SavedPlace, row = place.identity.row): Extract<ViewAnchor, { kind: "row" }> {
  return { kind: "row", row, offset: place.offset };
}

export function hasTailDeparture(entryKey: string | undefined, contentKey: string | undefined): boolean {
  const memory = readMemoryPlace(entryKey, contentKey);
  return memory !== null && "last" in memory;
}
