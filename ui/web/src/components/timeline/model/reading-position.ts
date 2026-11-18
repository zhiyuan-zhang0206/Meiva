import type { BrowserStorage } from "@/lib/browser/browser";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { saveScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import {
  findOldestTimelineCursor,
  parseItemIdParts,
} from "@/lib/timeline/timeline";

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

export interface ReloadReadingBookmark {
  readonly readingLocator?: string;
  readonly itemId: string;
  readonly rank: number;
  readonly viewportTop: number;
  readonly scrollTop: number;
  readonly usedCollapseReserve?: true;
  readonly keepSendReserve?: true;
  readonly savedAt: number;
}

export type ReloadRestoreConfig =
  | { readonly state: "disabled" }
  | { readonly state: "loading" }
  | {
      readonly state: "enabled";
      readonly session: BrowserStorage;
      readonly now: () => number;
      readonly maxOlderPages?: number;
      readonly maxOlderItems?: number;
    };

function isReloadReadingBookmark(value: unknown): value is ReloadReadingBookmark {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.itemId !== "string" ||
    candidate.itemId.length === 0 ||
    candidate.itemId.startsWith("_")
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
  if (candidate.usedCollapseReserve !== undefined && candidate.usedCollapseReserve !== true) {
    return false;
  }
  if (candidate.keepSendReserve !== undefined && candidate.keepSendReserve !== true) {
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
    valid.push([agentKey, raw]);
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

function sameBookmarkTarget(a: ReloadReadingBookmark, b: ReloadReadingBookmark): boolean {
  return (
    a.itemId === b.itemId &&
    a.rank === b.rank &&
    a.readingLocator === b.readingLocator &&
    Math.abs(a.viewportTop - b.viewportTop) < 0.5 &&
    Math.abs(a.scrollTop - b.scrollTop) < 0.5 &&
    a.usedCollapseReserve === b.usedCollapseReserve &&
    a.keepSendReserve === b.keepSendReserve &&
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
  lastWrittenCache.get(session)?.delete(agentKey);
  const remaining = readPrunedBookmarkEntries(session, now).filter(([key]) => key !== agentKey);
  if (remaining.length === 0) {
    session.remove(RELOAD_BOOKMARKS_STORAGE_KEY);
    return;
  }
  session.write(RELOAD_BOOKMARKS_STORAGE_KEY, JSON.stringify(Object.fromEntries(remaining)));
}

function findItemReadingLocator(
  itemId: string,
  rank: number,
  canonicalItems: readonly BackendTimelineItem[],
  compactBuffer: CompactTransitionBuffer | null,
): string | undefined {
  const buffered = compactBuffer?.rows.find(
    (row) => row.rank === rank && row.item.item_id === itemId,
  )?.item;
  const canonical = canonicalItems.find(
    (item) => item.item_id === itemId && (parseItemIdParts(item.item_id)?.rank ?? 0) === rank,
  );
  const locator = (buffered ?? canonical)?.reading_locator;
  return typeof locator === "string" && locator.length > 0 ? locator : undefined;
}

export function matchReloadBookmark(
  bookmark: ReloadReadingBookmark,
  canonicalItems: readonly BackendTimelineItem[],
  compactBuffer: CompactTransitionBuffer | null,
): { itemId: string; rank: number } | null {
  if (bookmark.readingLocator) {
    const bufferedByLocator = compactBuffer?.rows.find(
      (row) => row.item.reading_locator === bookmark.readingLocator,
    );
    if (bufferedByLocator) {
      return { itemId: bufferedByLocator.item.item_id, rank: bufferedByLocator.rank };
    }
    const canonicalByLocator = canonicalItems.find(
      (item) => item.reading_locator === bookmark.readingLocator,
    );
    if (canonicalByLocator) {
      return {
        itemId: canonicalByLocator.item_id,
        rank: parseItemIdParts(canonicalByLocator.item_id)?.rank ?? 0,
      };
    }
  }
  const bufferedById = compactBuffer?.rows.find(
    (row) =>
      row.rank === bookmark.rank &&
      row.item.item_id === bookmark.itemId &&
      (!bookmark.readingLocator || !row.item.reading_locator),
  );
  if (bufferedById) {
    return { itemId: bufferedById.item.item_id, rank: bufferedById.rank };
  }
  const canonicalById = canonicalItems.find(
    (item) =>
      item.item_id === bookmark.itemId &&
      (parseItemIdParts(item.item_id)?.rank ?? 0) === bookmark.rank &&
      (!bookmark.readingLocator || !item.reading_locator),
  );
  if (canonicalById) {
    return {
      itemId: canonicalById.item_id,
      rank: parseItemIdParts(canonicalById.item_id)?.rank ?? 0,
    };
  }
  return null;
}

export function reloadBookmarkToSavedScroll(
  contentKey: string,
  bookmark: ReloadReadingBookmark,
  matched?: { itemId: string; rank: number },
): SavedScroll {
  return {
    contentKey,
    scrollTop: bookmark.scrollTop,
    followBottom: false,
    ...(bookmark.usedCollapseReserve ? { usedCollapseReserve: true as const } : {}),
    ...(bookmark.keepSendReserve ? { keepSendReserve: true as const } : {}),
    anchor: {
      itemId: matched?.itemId ?? bookmark.itemId,
      rank: matched?.rank ?? bookmark.rank,
      viewportTop: bookmark.viewportTop,
    },
  };
}

export function oldestReloadCursorId(items: readonly BackendTimelineItem[]): string | null {
  return findOldestTimelineCursor(items)?.item_id ?? null;
}

// meiva:note
// Persist the current visible item to per-tab sessionStorage when cross-reload
// reading restore is enabled and the timeline actually overflows the viewport.
// meiva:link ui/web/src/docs/frontend-state/scroll-memory.md
export function rememberTimelineReloadPosition(
  viewport: HTMLElement,
  contentKey: string,
  canonicalItems: readonly BackendTimelineItem[],
  compactBuffer: CompactTransitionBuffer | null,
  reloadRestore: ReloadRestoreConfig,
  collapseReserve = 0,
  keepSendReserve = false,
): void {
  if (reloadRestore.state !== "enabled") return;
  if (viewport.scrollHeight <= viewport.clientHeight) return;
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    const id = item.dataset.itemId;
    if (!id || id.startsWith("_")) return false;
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  const itemId = node?.dataset.itemId;
  if (!node || !itemId) return;
  const rank = Number(node.dataset.displayRank ?? 0);
  const readingLocator = findItemReadingLocator(itemId, rank, canonicalItems, compactBuffer);
  const usedCollapseReserve =
    collapseReserve > 0 &&
    viewport.scrollTop + viewport.clientHeight > viewport.scrollHeight - collapseReserve + 1;
  const now = reloadRestore.now();
  saveReloadBookmark(
    reloadRestore.session,
    contentKey,
    {
      ...(readingLocator ? { readingLocator } : {}),
      itemId,
      rank,
      viewportTop: node.getBoundingClientRect().top - rect.top,
      scrollTop: Math.max(0, viewport.scrollTop),
      ...(usedCollapseReserve ? { usedCollapseReserve: true as const } : {}),
      ...(keepSendReserve ? { keepSendReserve: true as const } : {}),
      savedAt: now,
    },
    now,
  );
}

// meiva:note
// Leaving a conversation always saves the visible item and offset, including
// at the tail or between a stream chunk and its pin. Returning continues that
// reading position while newer output remains below it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function rememberTimelinePosition(
  viewport: HTMLElement, entryKey: string, contentKey: string,
  collapseReserve = 0, keepSendReserve = false,
) {
  const reserveMemory = keepSendReserve ? { keepSendReserve: true as const } : {};
  const rect = viewport.getBoundingClientRect();
  const node = [...viewport.querySelectorAll<HTMLElement>(
    ".timeline-item[data-item-id], [data-turn-expanded='false'][data-item-id]",
  )].find((item) => {
    const box = item.getBoundingClientRect();
    return box.bottom > rect.top + BAR_HEIGHT_PX && box.top < rect.bottom;
  });
  saveScrollMemory(entryKey, {
    contentKey, scrollTop: viewport.scrollTop, followBottom: false, ...reserveMemory,
    ...(collapseReserve > 0 && viewport.scrollTop + viewport.clientHeight > viewport.scrollHeight - collapseReserve + 1
      ? { usedCollapseReserve: true as const } : {}),
    anchor: node?.dataset.itemId ? {
      itemId: node.dataset.itemId, rank: Number(node.dataset.displayRank ?? 0),
      viewportTop: node.getBoundingClientRect().top - rect.top,
    } : undefined,
  });
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

// meiva:note
// Scroll keys and scrollbar drags resume normal following after a bookmark.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function watchReadingNavigation(viewport: HTMLElement, wrapper: HTMLElement | null, resume: () => void) {
  const keyDown = (event: KeyboardEvent) => {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) resume();
  };
  const pointerDown = (event: PointerEvent) => {
    const el = event.target as HTMLElement | null;
    if (el?.closest('[data-slot="scroll-area-scrollbar"]') && !el.closest('[data-activity-scroll="true"]')) {
      resume();
    }
  };
  viewport.addEventListener("keydown", keyDown);
  wrapper?.addEventListener("pointerdown", pointerDown, { capture: true });
  return () => {
    viewport.removeEventListener("keydown", keyDown);
    wrapper?.removeEventListener("pointerdown", pointerDown, { capture: true });
  };
}
