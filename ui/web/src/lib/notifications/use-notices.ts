// meiva:link ui/web/src/docs/frontend-components/fleet-view.md

"use client";

import { useQuery, type QueryClient } from "@tanstack/react-query";

import { useApi } from "../transport/transport-context";
import type { NoticeItem } from "../contracts/types";

// meiva:note
// The unresolved-questions query (GET /api/notices).
export const NOTICES_QUERY_KEY = ["notices"] as const;

// meiva:note
// Wire shape of GET /api/notices as cached under NOTICES_QUERY_KEY.
export interface NoticesFeedWire {
  awaiting: NoticeItem[];
}

export interface NoticesFeed {
  // meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.md
  // meiva:note
  // Unresolved questions across the fleet, oldest first.
  awaiting: NoticeItem[];
  // meiva:note
  // The fetch failed. Stale-while-error: `awaiting` keeps its last data; the
  //  view flags the failure so "no questions" is never indistinguishable from
  //  "couldn't load".
  error: boolean;
  /** First load in flight with nothing loaded yet. */
  isLoading: boolean;
}

/** Ids dropped optimistically whose server snapshot may still list them: one
 *  refetch initiated after the drop is enough to revive a row until the
 *  server resolve lands (task #3272). The deadline bounds how long a drop
 *  can hide a row when no confirmation ever arrives. */
const tombstoneState = {
  timers: new Map<number, ReturnType<typeof setTimeout>>(),
};
// KEEP (task #3696 exception inventory): the deadline gives one confirmation
// round trip (SSE resolve + refetch) room to land — 15s covers that several
// times over while keeping an unconfirmed drop from lingering.
const TOMBSTONE_MS = 15_000;

// meiva:note
// Drop tombstoned ids from one incoming snapshot. A snapshot that no longer
//  lists an id proves the server resolved it — retire that tombstone; the
//  deadline retires any tombstone no confirmation reaches.
function withoutDroppedRows(wire: NoticesFeedWire): NoticesFeedWire {
  const { timers } = tombstoneState;
  if (timers.size === 0) return wire;
  const listed = new Set(wire.awaiting.map((n) => n.id));
  for (const [id, timer] of [...timers.entries()]) {
    if (!listed.has(id)) {
      clearTimeout(timer);
      timers.delete(id);
    }
  }
  if (timers.size === 0) return wire;
  return { ...wire, awaiting: wire.awaiting.filter((n) => !timers.has(n.id)) };
}

export function useNotices(): NoticesFeed {
  const api = useApi();
  const feedQuery = useQuery({
    queryKey: NOTICES_QUERY_KEY,
    // meiva:note
    // The page size comes from the server default
    // (display.notices_open_default_limit, task #3696); the client pins no window.
    queryFn: async () => withoutDroppedRows(await api.getNotices()),
  });

  return {
    awaiting: feedQuery.data?.awaiting ?? [],
    error: feedQuery.isError,
    isLoading: feedQuery.isLoading,
  };
}

// meiva:note
// Remove questions from the cache — the instant local half of a resolve
//  (Task #1814). Waiting for the SSE notice_resolved round trip + the fold's
//  debounced refetch left an answered question visible ~2s after the click;
//  dropping it here makes the list reflect the answer immediately. The
//  SSE-triggered refetch reconciles a beat later (and is a no-op against
//  server truth).
//  Any in-flight refetch is cancelled first so a pre-resolve snapshot cannot
//  land over the drop and resurrect the row (task #3269), and the dropped ids
//  stay tombstoned for a beat so a refetch newly initiated after the drop
//  cannot either (task #3272).
export function dropOpenNotices(queryClient: QueryClient, noticeIds: number[]): void {
  if (noticeIds.length === 0) return;
  // Arm the tombstone before the optimistic drop: a refetch newly initiated
  // after this point (e.g. an SSE invalidation inside the resolve window)
  // still returns the pre-resolve snapshot, and would revive the rows for
  // the seconds until the resolve lands without this guard (task #3272).
  for (const id of noticeIds) {
    const existing = tombstoneState.timers.get(id);
    if (existing !== undefined) clearTimeout(existing);
    tombstoneState.timers.set(
      id,
      setTimeout(() => {
        tombstoneState.timers.delete(id);
      }, TOMBSTONE_MS),
    );
  }
  // meiva:note
  // Cancel the in-flight refetch first: its snapshot predates this drop, and
  // letting it land would resurrect the just-resolved row until the next
  // SSE-driven refetch corrects it — visible as the row bouncing back during a
  // resolve burst (task #3269). Same guard as the settings optimistic update
  // (use-user-settings.ts).
  void queryClient.cancelQueries({ queryKey: NOTICES_QUERY_KEY });
  const gone = new Set(noticeIds);
  queryClient.setQueryData<NoticesFeedWire>(NOTICES_QUERY_KEY, (old) => {
    if (!old) return old;
    return { ...old, awaiting: old.awaiting.filter((n) => !gone.has(n.id)) };
  });
}

export function dropTombstonesResetForTest(): void {
  for (const timer of tombstoneState.timers.values()) clearTimeout(timer);
  tombstoneState.timers.clear();
}
