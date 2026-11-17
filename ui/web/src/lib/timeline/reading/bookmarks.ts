import type { BrowserStorage } from "@/lib/browser/browser";
import type { SavedScroll } from "@/lib/layout/scroll-memory";

export const READING_BOOKMARKS_KEY = "timeline-bookmarks:v1";
export const READING_BOOKMARK_MAX = 64;
export const READING_BOOKMARK_TTL_MS = 7 * 24 * 60 * 60 * 1000;

export interface ReadingBookmark {
  agentId: number;
  savedAt: number;
  locator: string;
  position: SavedScroll & { anchor: NonNullable<SavedScroll["anchor"]> };
}

type BookmarksRead =
  | { state: "ready"; entries: ReadingBookmark[] }
  | { state: "invalid" | "unavailable" };

export type ReadingBookmarkRead =
  | { state: "ready"; bookmark: ReadingBookmark | null }
  | { state: "invalid" | "unavailable" };

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function integer(value: unknown): value is number {
  return finite(value) && Number.isSafeInteger(value);
}

function position(value: unknown, agentId: number): ReadingBookmark["position"] | null {
  if (!object(value) || value.contentKey !== String(agentId) || !finite(value.scrollTop) ||
    value.scrollTop < 0 || !object(value.anchor)) return null;
  const anchor = value.anchor;
  if (typeof anchor.itemId !== "string" || !anchor.itemId || anchor.itemId.length > 512 ||
    !integer(anchor.rank) || anchor.rank < 0 || !finite(anchor.viewportTop)) return null;
  return {
    contentKey: String(agentId), scrollTop: value.scrollTop, followBottom: false,
    anchor: { itemId: anchor.itemId, rank: anchor.rank, viewportTop: anchor.viewportTop },
    ...(value.usedCollapseReserve === true ? { usedCollapseReserve: true as const } : {}),
    ...(value.keepSendReserve === true ? { keepSendReserve: true as const } : {}),
  };
}

function bookmark(value: unknown): ReadingBookmark | null {
  if (!object(value) || !integer(value.agentId) || value.agentId < 1 || !finite(value.savedAt) ||
    value.savedAt < 0 || typeof value.locator !== "string" || value.locator.length > 4096 ||
    !/^[A-Za-z0-9_-]+$/.test(value.locator)) return null;
  const saved = position(value.position, value.agentId);
  return saved ? { agentId: value.agentId, savedAt: value.savedAt, locator: value.locator, position: saved } : null;
}

// meiva:note
// Read failures and unknown versions retain their original bytes. A read never
// prunes storage; only a successful metadata write retires expired entries.
// meiva:link ui/web/src/docs/frontend-state/timeline-cache.md
function entries(storage: BrowserStorage, now: number): BookmarksRead {
  if (!finite(now) || now < 0) return { state: "invalid" };
  const raw = storage.read(READING_BOOKMARKS_KEY);
  if (raw.state === "unavailable") return raw;
  if (raw.state === "absent") return { state: "ready", entries: [] };
  try {
    const value: unknown = JSON.parse(raw.value);
    if (!object(value) || value.version !== 1 || !Array.isArray(value.entries)) return { state: "invalid" };
    const clean: ReadingBookmark[] = [];
    for (const item of value.entries) {
      const saved = bookmark(item);
      if (!saved) return { state: "invalid" };
      if (saved.savedAt > now) return { state: "invalid" };
      if (now - saved.savedAt < READING_BOOKMARK_TTL_MS) clean.push(saved);
    }
    const unique = new Map<number, ReadingBookmark>();
    for (const saved of clean.sort((a, b) => b.savedAt - a.savedAt)) {
      if (!unique.has(saved.agentId)) unique.set(saved.agentId, saved);
    }
    return { state: "ready", entries: [...unique.values()].slice(0, READING_BOOKMARK_MAX) };
  } catch {
    return { state: "invalid" };
  }
}

export function readReadingBookmark(storage: BrowserStorage, agentId: number, now: number): ReadingBookmarkRead {
  const read = entries(storage, now);
  return read.state === "ready"
    ? { state: "ready", bookmark: read.entries.find((saved) => saved.agentId === agentId) ?? null }
    : read;
}

export function saveReadingBookmark(
  storage: BrowserStorage, value: ReadingBookmark, now: number,
): "stored" | "invalid" | "unavailable" {
  const saved = bookmark(value);
  if (!saved || saved.savedAt > now || now - saved.savedAt >= READING_BOOKMARK_TTL_MS) return "invalid";
  const read = entries(storage, now);
  if (read.state !== "ready") return read.state;
  const retained = read.entries.filter((entry) => entry.agentId !== saved.agentId);
  return storage.write(READING_BOOKMARKS_KEY, JSON.stringify({
    version: 1, entries: [saved, ...retained].slice(0, READING_BOOKMARK_MAX),
  }));
}
