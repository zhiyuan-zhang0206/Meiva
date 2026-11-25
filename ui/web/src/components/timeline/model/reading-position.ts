import type { BrowserStorage } from "@/lib/browser/browser";
import { isDisplayOnlyItemId } from "@/lib/timeline/timeline";

// meiva:note
// Bounded per-tab sessionStorage cache for cross-reload reading bookmarks:
// 64 agents caps the serialized JSON map to ~8 KB (well inside the 5 MiB Web
// Storage quota) while covering multi-agent tab rotation; 7 days evicts stale
// bookmarks from long-lived tabs whose conversation windows have moved on.
// meiva:link ui/web/src/docs/frontend-state/scroll-memory.md
export const RELOAD_BOOKMARKS_STORAGE_KEY = "meiva.timeline.reading-bookmarks.v1";
export const RELOAD_BOOKMARK_MAX_AGENTS = 64;
export const RELOAD_BOOKMARK_TTL_MS = 7 * 24 * 60 * 60 * 1000;

// meiva:note
// Bounded background older-page backfill during cross-reload bookmark search:
// 5 pages (matching REVEAL_MAX_OLDER_PAGES) caps cold-reload request latency,
// and 250 prepended items (matching the default retained-items ceiling) bounds
// timeline memory before starting additional history fetches.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export const RELOAD_RESTORE_MAX_OLDER_PAGES = 5;
export const RELOAD_RESTORE_MAX_OLDER_ITEMS = 250;
export const RELOAD_RESTORE_SEARCH_TIMEOUT_MS = 10_000;

export interface ReloadReadingBookmark {
  readonly readingLocator?: string;
  readonly itemId: string;
  readonly rank: number;
  readonly turn?: true;
  readonly viewportTop: number;
  readonly scrollTop: number;
  readonly savedAt: number;
}

export type ReloadRestoreConfig =
  | { readonly state: "disabled" }
  | { readonly state: "loading" }
  | {
      readonly state: "enabled";
      readonly session: BrowserStorage;
      readonly now: () => number;
    };

function isReloadReadingBookmark(value: unknown): value is ReloadReadingBookmark {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.itemId !== "string" ||
    candidate.itemId.length === 0 ||
    isDisplayOnlyItemId(candidate.itemId)
  ) {
    return false;
  }
  if (
    typeof candidate.rank !== "number" ||
    !Number.isInteger(candidate.rank) ||
    candidate.rank < 0
  ) {
    return false;
  }
  if (typeof candidate.viewportTop !== "number" || !Number.isFinite(candidate.viewportTop)) {
    return false;
  }
  if (
    typeof candidate.scrollTop !== "number" ||
    !Number.isFinite(candidate.scrollTop) ||
    candidate.scrollTop < 0
  ) {
    return false;
  }
  if (typeof candidate.savedAt !== "number" || !Number.isFinite(candidate.savedAt)) {
    return false;
  }
  if (
    candidate.readingLocator !== undefined &&
    (typeof candidate.readingLocator !== "string" || candidate.readingLocator.length === 0)
  ) {
    return false;
  }
  if (candidate.turn !== undefined && candidate.turn !== true) {
    return false;
  }
  return true;
}

function readPrunedBookmarkEntries(
  session: BrowserStorage,
  now: number,
): [string, ReloadReadingBookmark][] {
  const stored = session.read(RELOAD_BOOKMARKS_STORAGE_KEY);
  if (stored.state !== "found") return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(stored.value);
  } catch {
    return [];
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return [];
  const valid: [string, ReloadReadingBookmark][] = [];
  for (const [agentKey, raw] of Object.entries(parsed)) {
    if (!agentKey || !isReloadReadingBookmark(raw)) continue;
    if (now - raw.savedAt > RELOAD_BOOKMARK_TTL_MS) continue;
    const { itemId, rank, turn, readingLocator, viewportTop, scrollTop, savedAt } = raw;
    valid.push([agentKey, {
      itemId, rank, viewportTop, scrollTop, savedAt,
      ...(turn ? { turn } : {}),
      ...(readingLocator ? { readingLocator } : {}),
    }]);
  }
  valid.sort((a, b) => b[1].savedAt - a[1].savedAt);
  return valid.slice(0, RELOAD_BOOKMARK_MAX_AGENTS);
}

export function readReloadBookmark(
  session: BrowserStorage,
  agentKey: string,
  now: number,
): ReloadReadingBookmark | null {
  const entries = readPrunedBookmarkEntries(session, now);
  const found = entries.find(([key]) => key === agentKey);
  return found ? found[1] : null;
}

const lastWrittenCache = new WeakMap<BrowserStorage, Map<string, ReloadReadingBookmark>>();
const knownClearedCache = new WeakMap<BrowserStorage, Set<string>>();

function sameBookmarkTarget(a: ReloadReadingBookmark, b: ReloadReadingBookmark): boolean {
  return (
    a.itemId === b.itemId &&
    a.rank === b.rank &&
    a.turn === b.turn &&
    a.readingLocator === b.readingLocator &&
    Math.abs(a.viewportTop - b.viewportTop) < 0.5 &&
    Math.abs(a.scrollTop - b.scrollTop) < 0.5 &&
    Math.abs(a.savedAt - b.savedAt) < 60_000
  );
}

export function saveReloadBookmark(
  session: BrowserStorage,
  agentKey: string,
  bookmark: ReloadReadingBookmark,
  now: number,
): void {
  if (!agentKey || !isReloadReadingBookmark(bookmark)) return;
  knownClearedCache.get(session)?.delete(agentKey);
  let perSession = lastWrittenCache.get(session);
  if (!perSession) {
    perSession = new Map();
    lastWrittenCache.set(session, perSession);
  }
  const cached = perSession.get(agentKey);
  if (cached && sameBookmarkTarget(cached, bookmark)) return;

  const existing = readPrunedBookmarkEntries(session, now);
  const prev = existing.find(([key]) => key === agentKey)?.[1];
  if (prev && sameBookmarkTarget(prev, bookmark)) {
    perSession.set(agentKey, prev);
    return;
  }
  const head: [string, ReloadReadingBookmark] = [agentKey, bookmark];
  const merged = [head, ...existing.filter(([key]) => key !== agentKey)]
    .sort((a, b) => b[1].savedAt - a[1].savedAt)
    .slice(0, RELOAD_BOOKMARK_MAX_AGENTS);
  if (session.write(RELOAD_BOOKMARKS_STORAGE_KEY, JSON.stringify(Object.fromEntries(merged))) === "stored") {
    perSession.set(agentKey, bookmark);
  }
}

export function removeReloadBookmark(
  session: BrowserStorage,
  agentKey: string,
  now: number,
): void {
  if (!agentKey) return;
  let cleared = knownClearedCache.get(session);
  if (cleared?.has(agentKey)) return;
  lastWrittenCache.get(session)?.delete(agentKey);
  const entries = readPrunedBookmarkEntries(session, now);
  if (!entries.some(([key]) => key === agentKey)) {
    if (!cleared) {
      cleared = new Set();
      knownClearedCache.set(session, cleared);
    }
    cleared.add(agentKey);
    return;
  }
  const remaining = entries.filter(([key]) => key !== agentKey);
  const status =
    remaining.length === 0
      ? session.remove(RELOAD_BOOKMARKS_STORAGE_KEY)
      : session.write(RELOAD_BOOKMARKS_STORAGE_KEY, JSON.stringify(Object.fromEntries(remaining)));
  if (status === "stored" || status === "removed") {
    if (!cleared) {
      cleared = new Set();
      knownClearedCache.set(session, cleared);
    }
    cleared.add(agentKey);
  }
}

interface ReadingExpansion {
  items: ReadonlyMap<string, boolean>;
  turns: ReadonlyMap<string, boolean>;
}
// meiva:note
// Only ephemeral view choices live here. Server rows stay in the QueryClient.
// meiva:link ui/web/src/docs/frontend-state/scroll-memory.md
const expansions = new Map<string, ReadingExpansion>();
export function readTimelineExpansion(key: string | undefined) {
  return key ? expansions.get(key) : undefined;
}
export function rememberTimelineExpansion(key: string | undefined, value: ReadingExpansion) {
  if (key) expansions.set(key, value);
}
