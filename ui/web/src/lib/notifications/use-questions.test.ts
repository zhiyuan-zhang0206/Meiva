// meiva:note
// useQuestions hook tests — the unresolved-questions data contract. GET /api/questions
// carries the fleet's unresolved questions in `awaiting`. Locks the invalidation
// policy: question_asked and question_resolved refetch the query, a reconnect refetches
// the fold-owned snapshot, and a failed fetch surfaces instead of reading as "no
// questions". The policy lives in the FOLD (lib/fold/questions) inside the real
// EventStreamProvider — these tests drive the fold through a stubbed EventSource,
// exactly like use-agents.test.ts.

// meiva:note
// @vitest-environment happy-dom

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { QuestionItem, QuestionsFeed } from "../contracts/types";
import { realNetwork, startGateway, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { eventStreamHarness, type EventStreamHarness } from "../transport/tests/stream/event-stream-harness";
import {
  QUESTIONS_QUERY_KEY,
  dropTombstonesResetForTest,
  dropOpenQuestions,
  useQuestions,
  type QuestionsFeedWire,
} from "./use-questions";

function question(id: number, title: string): QuestionItem {
  return {
    id,
    agent_id: 1,
    agent_label: null,
    title,
    content: null,
    created_at: "2026-06-14T00:00:00Z",
  };
}

function feed(awaiting: QuestionItem[] = []): QuestionsFeed {
  return { awaiting };
}

let desk: { awaiting: QuestionItem[] };
let failureStatus: number | null;
let gateway: TestGateway;

let queryClient: QueryClient;
let stream: EventStreamHarness;

beforeEach(async () => {
  dropTombstonesResetForTest();
  desk = { awaiting: [] };
  failureStatus = null;
  gateway = await startGateway(realNetwork.createServer, (request) => {
    const url = new URL(request.path, "http://gateway");
    if (url.pathname !== "/api/questions") return { kind: "text", status: 404, body: "unexpected route" };
    if (failureStatus !== null) return { kind: "text", status: failureStatus, body: "unavailable" };
    return { kind: "json", body: feed(desk.awaiting) };
  });
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  stream = eventStreamHarness({ api: gatewayRig(gateway).api, queryClient, wallNow: () => Date.now() });
});
afterEach(async () => {
  cleanup();
  vi.useRealTimers();
  await gateway.close();
});

function ids(items: readonly QuestionItem[]): number[] {
  return items.map((n) => n.id);
}

function awaitingIds(): number[] | undefined {
  return queryClient.getQueryData<QuestionsFeedWire>(QUESTIONS_QUERY_KEY)?.awaiting.map((n) => n.id);
}

describe("useQuestions", () => {
  it("fetches the unresolved questions on mount", async () => {
    desk.awaiting = [question(2, "decision")];
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([2]));
  });

  it("question_resolved refetches the questions", async () => {
    desk.awaiting = [question(7, "q")];
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([7]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.awaiting = [];
    stream.deliver({ role: "question_resolved", agent_id: 1, question_id: 7 });

    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([]));
  });

  it("question_asked refetches the questions", async () => {
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.awaiting = [question(8, "q")];
    stream.deliver({ role: "question_asked", agent_id: 1, question_id: 8, title: "q" });

    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([8]));
  });

  it("reconnect (open) refetches the questions missed while disconnected", async () => {
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await stream.whenSourceExists();

    desk.awaiting = [question(1, "missed question")];
    stream.open();

    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([1]));
  });

  // meiva:note
  // Audit C3: failures must surface (stale-while-error), not silently degrade
  // to an empty list — the view cannot distinguish "empty" from "broken".
  it("a failed fetch surfaces error=true with an empty list", async () => {
    failureStatus = 503;
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.awaiting).toEqual([]);
  });
});

describe("dropOpenQuestions (Task #1814)", () => {
  it("filters the resolved ids out of the questions, keeping everything else", () => {
    // meiva:note
    // Wire shape under QUESTIONS_QUERY_KEY (the api.getQuestions data).
    queryClient.setQueryData(QUESTIONS_QUERY_KEY, {
      awaiting: [
        { id: 3, title: "drop" },
        { id: 4, title: "keep" },
      ],
    });

    // meiva:note
    // A mix of a matched id and an unknown id — only the matched row vanishes.
    dropOpenQuestions(queryClient, [3, 99]);

    expect(awaitingIds()).toEqual([4]);
  });

  it("is a no-op on an empty id list and on an unseeded cache", () => {
    dropOpenQuestions(queryClient, []);
    expect(queryClient.getQueryData(QUESTIONS_QUERY_KEY)).toBeUndefined();

    dropOpenQuestions(queryClient, [1]);
    expect(queryClient.getQueryData(QUESTIONS_QUERY_KEY)).toBeUndefined();
  });

  it("cancels the in-flight refetch so a pre-resolve snapshot cannot resurrect a dropped row (task #3269)", async () => {
    queryClient.setQueryData(QUESTIONS_QUERY_KEY, {
      awaiting: [
        { id: 1, title: "resolved" },
        { id: 2, title: "next" },
      ],
    });

    // meiva:note
    // A refetch is mid-flight (e.g. a previous resolve's SSE-driven refetch);
    // its response carries the pre-resolve snapshot — question 1 still present.
    let release!: () => void;
    const inFlight = new Promise<QuestionsFeed>((resolve) => {
      release = () => resolve(feed([question(1, "resolved"), question(2, "next")]));
    });
    let fetches = 0;
    const fetchPromise = queryClient.query({
      queryKey: QUESTIONS_QUERY_KEY,
      queryFn: () => {
        fetches += 1;
        return inFlight;
      },
    });
    fetchPromise.catch(() => undefined);
    // meiva:note
    // Confirm the refetch is genuinely in flight before the drop (a cache
    // short-circuit would make this test green for the wrong reason).
    await vi.waitFor(() => expect(queryClient.isFetching({ queryKey: QUESTIONS_QUERY_KEY })).toBe(1));

    // meiva:note
    // The user resolves question 1 while that refetch is in flight.
    dropOpenQuestions(queryClient, [1]);
    expect(awaitingIds()).toEqual([2]);

    release();
    // meiva:note
    // Wait for the fetch to SETTLE before asserting — checking too early would
    // pass before a late stale snapshot lands.
    await vi.waitFor(() => expect(queryClient.isFetching({ queryKey: QUESTIONS_QUERY_KEY })).toBe(0));
    expect(fetches).toBe(1);
    expect(awaitingIds()).toEqual([2]);
  });

  it("hides a snapshot initiated after the drop until the server stops listing the row (task #3272)", async () => {
    desk.awaiting = [question(1, "a"), question(2, "b")];
    queryClient.setQueryData(QUESTIONS_QUERY_KEY, feed(desk.awaiting));
    const { result } = renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(ids(result.current.awaiting)).toEqual([1, 2]));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // meiva:note
    // The user resolves question 1 (optimistic drop + tombstone).
    dropOpenQuestions(queryClient, [1]);
    expect(awaitingIds()).toEqual([2]);

    // meiva:note
    // A refetch STARTING after the drop still returns the pre-resolve
    // snapshot (the server has not processed the resolve yet) — the row must
    // stay hidden.
    await queryClient.refetchQueries({ queryKey: QUESTIONS_QUERY_KEY });
    expect(awaitingIds()).toEqual([2]);

    // meiva:note
    // The post-resolve snapshot no longer lists the row: the tombstone
    // retires, so a later snapshot is trusted again.
    desk.awaiting = [question(2, "b")];
    await queryClient.refetchQueries({ queryKey: QUESTIONS_QUERY_KEY });
    desk.awaiting = [question(1, "a"), question(2, "b")];
    await queryClient.refetchQueries({ queryKey: QUESTIONS_QUERY_KEY });
    expect(awaitingIds()).toEqual([1, 2]);
  });

  it("an expired tombstone stops hiding a row the server keeps listing (task #3272)", async () => {
    desk.awaiting = [question(7, "a")];
    queryClient.setQueryData(QUESTIONS_QUERY_KEY, feed(desk.awaiting));
    renderHook(() => useQuestions(), { wrapper: stream.wrapper });
    await waitFor(() => expect(awaitingIds()).toEqual([7]));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    vi.useFakeTimers();
    dropOpenQuestions(queryClient, [7]);
    vi.advanceTimersByTime(20_000);
    vi.useRealTimers();
    await queryClient.refetchQueries({ queryKey: QUESTIONS_QUERY_KEY });
    // meiva:note
    // Past the 15s bound the guard releases: a resolve that never landed
    // server-side cannot hide an open row forever.
    expect(awaitingIds()).toEqual([7]);
  });
});
