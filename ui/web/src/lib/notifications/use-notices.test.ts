// useNotices hook tests — the Inbox panel's single data contract (Task #1024,
// R4 layer 2, Q1=A). GET /api/notices carries open (FYI) + awaiting
// (require_response) + one keyset page of the resolved history. Locks the
// cross-table invariant the unified inbox depends on: notice_resolved
// invalidates BOTH the open queue (a resolution can move a row out of open or
// awaiting) AND the resolved history, notice_posted refreshes only the open
// queue, and a reconnect refetches the fold-owned snapshots. Since R4 layer 1
// the invalidation policy lives in the FOLD (lib/fold/notices) inside the real
// EventStreamProvider — these tests drive the fold through a stubbed
// EventSource, exactly like use-agents.test.ts.

// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NoticeItem, NoticesFeed } from "../contracts/types";
import { realNetwork, startGateway, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { eventStreamHarness, type EventStreamHarness } from "../transport/tests/stream/event-stream-harness";
import {
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
  dropTombstonesResetForTest,
  dropOpenNotices,
  useNotices,
  type NoticesFeedWire,
} from "./use-notices";

function notice(id: number, title: string): NoticeItem {
  return {
    id,
    agent_id: 1,
    agent_label: null,
    title,
    content: null,
    priority: "P2",
    require_response: false,
    blocking: false,
    created_at: "2026-06-14T00:00:00Z",
    expire_at: "2026-06-15T00:00:00Z",
  };
}

function feed(over: Partial<NoticesFeed> = {}): NoticesFeed {
  return {
    open: [],
    awaiting: [],
    resolved_page: [],
    next_cursor: null,
    ...over,
  };
}

let desk: {
  open: NoticeItem[];
  awaiting: NoticeItem[];
  resolved: NoticeItem[];
  nextCursor: NoticesFeed["next_cursor"];
  older: NoticeItem[];
};
let failureStatus: number | null;
let gateway: TestGateway;

let queryClient: QueryClient;
let stream: EventStreamHarness;

beforeEach(async () => {
  dropTombstonesResetForTest();
  desk = { open: [], awaiting: [], resolved: [], nextCursor: null, older: [] };
  failureStatus = null;
  gateway = await startGateway(realNetwork.createServer, (request) => {
    const url = new URL(request.path, "http://gateway");
    if (url.pathname !== "/api/notices") return { kind: "text", status: 404, body: "unexpected route" };
    if (failureStatus !== null) return { kind: "text", status: failureStatus, body: "unavailable" };
    return { kind: "json", body: url.searchParams.has("before_at")
      ? feed({ resolved_page: desk.older })
      : feed({ open: desk.open, awaiting: desk.awaiting, resolved_page: desk.resolved, next_cursor: desk.nextCursor }) };
  });
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  stream = eventStreamHarness({ api: gatewayRig(gateway).api, queryClient, wallNow: () => Date.now() });
});
afterEach(async () => {
  cleanup();
  vi.useRealTimers();
  await gateway.close();
});

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 250));
}

function ids(items: readonly NoticeItem[]): number[] {
  return items.map((n) => n.id);
}

function openIds(): number[] | undefined {
  return queryClient.getQueryData<NoticesFeedWire>(NOTICES_QUERY_KEY)?.open.map((n) => n.id);
}

describe("useNotices", () => {
  it("fetches the unified feed on mount and splits open/awaiting/resolved", async () => {
    desk.open = [notice(1, "fyi")];
    desk.awaiting = [notice(2, "decision")];
    desk.resolved = [notice(3, "done")];
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.open)).toEqual([1]));
    expect(ids(result.current.awaiting)).toEqual([2]);
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([3]));
  });

  it("notice_resolved invalidates BOTH the open queue and the resolved history", async () => {
    desk.open = [notice(7, "fyi")];
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.open)).toEqual([7]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.open = [];
    desk.resolved = [notice(7, "fyi")];
    stream.deliver({ role: "notice_resolved", agent_id: 1, notice_id: 7 });

    await waitFor(() => expect(ids(result.current.open)).toEqual([]));
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([7]));
  });

  it("notice_posted invalidates only the open queue, not the resolved history", async () => {
    desk.resolved = [notice(5, "done")];
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([5]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.open = [notice(8, "fyi")];
    desk.resolved = [notice(5, "done"), notice(9, "unrequested")];
    stream.deliver({ role: "notice_posted", agent_id: 1, notice_id: 8, priority: "P2", title: "fyi", task_id: null });

    await waitFor(() => expect(ids(result.current.open)).toEqual([8]));
    await settle();
    expect(ids(result.current.resolved)).toEqual([5]);
  });

  it("reconnect (open) refetches both the open queue and the resolved history", async () => {
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.open = [notice(1, "missed fyi")];
    desk.resolved = [notice(2, "missed history")];
    stream.open();

    await waitFor(() => expect(ids(result.current.open)).toEqual([1]));
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([2]));
  });

  // Audit C3: failures must surface (stale-while-error), not silently degrade
  // to an empty list — the queue cannot distinguish "empty" from "broken".
  it("a failed open-queue fetch surfaces error=true with an empty list", async () => {
    // meiva:note
    // The history is already cached; only the cold open queue needs this HTTP response.
    queryClient.setQueryDefaults(NOTICES_RESOLVED_QUERY_KEY, { staleTime: Infinity });
    queryClient.setQueryData(NOTICES_RESOLVED_QUERY_KEY, { pages: [feed()], pageParams: [undefined] });
    failureStatus = 503;
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.open).toEqual([]);
    expect(result.current.awaiting).toEqual([]);
  });

  it("a failed resolved-history fetch surfaces resolvedError=true with an empty list", async () => {
    desk.open = [notice(1, "fyi")];
    // meiva:note
    // Both initial reads use the same wire URL. Keep the open queue warm so this
    // real HTTP refusal belongs to the cold history, rather than inventing a JS-only route.
    queryClient.setQueryDefaults(NOTICES_QUERY_KEY, { staleTime: Infinity });
    queryClient.setQueryData(NOTICES_QUERY_KEY, feed({ open: desk.open }));
    failureStatus = 503;
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.resolvedError).toBe(true));
    expect(result.current.resolved).toEqual([]);
    expect(result.current.error).toBe(false);
    expect(ids(result.current.open)).toEqual([1]);
  });

  it("resolved history pages append via next_cursor", async () => {
    desk.resolved = [notice(5, "newest")];
    desk.nextCursor = { before_at: "2026-06-14T02:00:00Z", before_id: 5 };
    desk.older = [notice(4, "older")];
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([5]));
    act(() => result.current.fetchNextPage());
    await waitFor(() => expect(ids(result.current.resolved)).toEqual([5, 4]));
    const cursors = gateway.requests.map((request) => new URL(request.path, gateway.origin))
      .filter((url) => url.searchParams.has("before_at"));
    expect(cursors).toHaveLength(1);
    expect(cursors[0].searchParams.get("before_at")).toBe("2026-06-14T02:00:00Z");
    expect(cursors[0].searchParams.get("before_id")).toBe("5");
  });
});

describe("dropOpenNotices (Task #1814)", () => {
  it("filters the resolved ids out of open AND awaiting, keeping everything else", () => {
    // Wire shape under NOTICES_QUERY_KEY (camelCase, the api.getNotices data).
    queryClient.setQueryData(NOTICES_QUERY_KEY, {
      open: [
        { id: 1, title: "keep fyi" },
        { id: 2, title: "drop fyi" },
      ],
      awaiting: [
        { id: 3, title: "drop decision" },
        { id: 4, title: "keep decision" },
      ],
      resolved_page: [{ id: 5, title: "history untouched" }],
      next_cursor: { before_at: "2026-06-14T00:00:00Z", before_id: 5 },
    });

    // A mix of ids spanning both lists plus an unknown id — only the matched
    // rows vanish; the resolved history and the cursor are never touched.
    dropOpenNotices(queryClient, [2, 3, 99]);

    const data = queryClient.getQueryData<NoticesFeedWire>(NOTICES_QUERY_KEY);
    expect(data?.open.map((n) => n.id)).toEqual([1]);
    expect(data?.awaiting.map((n) => n.id)).toEqual([4]);
    expect(data?.resolved_page.map((n) => n.id)).toEqual([5]);
    expect(data?.next_cursor?.before_id).toBe(5);
  });

  it("is a no-op on an empty id list and on an unseeded cache", () => {
    dropOpenNotices(queryClient, []);
    expect(queryClient.getQueryData(NOTICES_QUERY_KEY)).toBeUndefined();

    dropOpenNotices(queryClient, [1]);
    expect(queryClient.getQueryData(NOTICES_QUERY_KEY)).toBeUndefined();
  });

  it("cancels the in-flight open-queue refetch so a pre-resolve snapshot cannot resurrect a dropped row (task #3269)", async () => {
    queryClient.setQueryData(NOTICES_QUERY_KEY, {
      open: [
        { id: 1, title: "resolved" },
        { id: 2, title: "next" },
      ],
      awaiting: [],
      resolved_page: [],
      next_cursor: null,
    });

    // A refetch is mid-flight (e.g. a previous resolve's SSE-driven refetch);
    // its response carries the pre-resolve snapshot — notice 1 still present.
    let release!: () => void;
    const inFlight = new Promise<NoticesFeed>((resolve) => {
      release = () =>
        resolve(feed({ open: [notice(1, "resolved"), notice(2, "next")] }));
    });
    let fetches = 0;
    const fetchPromise = queryClient.query({
      queryKey: NOTICES_QUERY_KEY,
      queryFn: () => {
        fetches += 1;
        return inFlight;
      },
    });
    fetchPromise.catch(() => undefined); // a cancelled fetch rejects; handled here
    // Confirm the refetch is genuinely in flight before the drop (a cache
    // short-circuit would make this test green for the wrong reason).
    await vi.waitFor(() => expect(queryClient.isFetching({ queryKey: NOTICES_QUERY_KEY })).toBe(1));

    // The user resolves notice 1 while that refetch is in flight.
    dropOpenNotices(queryClient, [1]);
    expect(openIds()).toEqual([2]);

    release();
    // Wait for the fetch to SETTLE before asserting — checking too early would
    // pass before a late stale snapshot lands.
    await vi.waitFor(() => expect(queryClient.isFetching({ queryKey: NOTICES_QUERY_KEY })).toBe(0));
    expect(fetches).toBe(1);
    expect(openIds()).toEqual([2]);
  });

  it("hides a snapshot initiated after the drop until the server stops listing the row (task #3272)", async () => {
    desk.open = [notice(1, "a"), notice(2, "b")];
    queryClient.setQueryData(NOTICES_QUERY_KEY, feed({ open: desk.open }));
    const { result } = renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.open)).toEqual([1, 2]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // The user resolves notice 1 (optimistic drop + tombstone).
    dropOpenNotices(queryClient, [1]);
    expect(openIds()).toEqual([2]);

    // A refetch STARTING after the drop still returns the pre-resolve
    // snapshot (the server has not processed the resolve yet) — the row must
    // stay hidden.
    await queryClient.refetchQueries({ queryKey: NOTICES_QUERY_KEY });
    expect(openIds()).toEqual([2]);

    // The post-resolve snapshot no longer lists the row: the tombstone
    // retires, so a later snapshot is trusted again.
    desk.open = [notice(2, "b")];
    await queryClient.refetchQueries({ queryKey: NOTICES_QUERY_KEY });
    desk.open = [notice(1, "a"), notice(2, "b")];
    await queryClient.refetchQueries({ queryKey: NOTICES_QUERY_KEY });
    expect(openIds()).toEqual([1, 2]);
  });

  it("an expired tombstone stops hiding a row the server keeps listing (task #3272)", async () => {
    desk.open = [notice(7, "a")];
    queryClient.setQueryData(NOTICES_QUERY_KEY, feed({ open: desk.open }));
    renderHook(() => useNotices(), { wrapper: stream.wrapper });
    await waitFor(() => expect(openIds()).toEqual([7]));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    vi.useFakeTimers();
    dropOpenNotices(queryClient, [7]);
    vi.advanceTimersByTime(20_000);
    vi.useRealTimers();
    await queryClient.refetchQueries({ queryKey: NOTICES_QUERY_KEY });
    // Past the 15s bound the guard releases: a resolve that never landed
    // server-side cannot hide an open row forever.
    expect(openIds()).toEqual([7]);
  });
});
