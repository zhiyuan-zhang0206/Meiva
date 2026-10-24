// De-quarantined 2026-08-24. The historical CI flake asserted a Zustand store
// notification count for a frame batch; React batching makes that an
// implementation detail. The test now asserts the final timeline state.
//
// useTimeline hook integration tests — React Testing Library + happy-dom.
//
// Complements timeline-scenarios.test.ts:
//   - timeline-scenarios: simulator runs the pure reducer + merge
//     chain, no React.
//   - here:                renderHook actually runs React state (useState /
//                          useEffect / useRef) and covers internal hook
//                          wiring (reload trigger timing, ref resets,
//                          agentId-switch effect ordering, cancelled flag
//                          races, etc.).
//
// Mock strategy:
//   - useEventStream: mocked as a helper that exposes onEvent so the
//     test can manually trigger SSE events (the real hook builds an
//     EventSource over the network; tests don't need that).

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../transport/api";
import { apiWrapper, fakeApi } from "../transport/tests/api/fake-api";
import { interactionTimingResetForTest } from "../format/interaction-timing";
import { SETTINGS_QUERY_KEY } from "../state/use-user-settings";
import type {
  Answer,
  SnapshotRead,
  TimelineRead,
  TimelineReadOptions,
  UseTimelineTestContext,
} from "./tests/use-timeline-context";
import { registerUseTimelinePaginationTests } from "./tests/use-timeline-pagination";
import {
  USER_SETTING_DEFAULTS,
  type BackendTimelineItem,
  type ConversationSnapshotResponse,
  type SystemEvent,
  type TimelineResponse,
} from "../contracts/types";
import { parseItemId } from "./timeline";
import { useTimelineStore } from "./timeline-store";
import { useTimeline } from "./use-timeline";
import type { ConnectionEvent } from "../transport/useEventStream";

// -- mock layer ────────────────────────────────────────────────────────────

const startTransitionSpy = vi.fn((cb: () => void) => cb());
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof React>();
  return {
    ...actual,
    startTransition: (cb: () => void) => startTransitionSpy(cb),
  };
});

let currentEventHandler: ((ev: SystemEvent) => void) | null = null;
let currentConnectionHandler: ((ev: ConnectionEvent) => void) | null = null;
let currentBatchHandler: ((evs: SystemEvent[]) => void) | null = null;
vi.mock("../transport/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  AgentEventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useAgentEventStream: (
    onEvent: (ev: SystemEvent) => void,
    onConnectionEvent: (ev: ConnectionEvent) => void,
    onEventBatch?: (evs: SystemEvent[]) => void,
  ) => {
    currentEventHandler = onEvent;
    currentConnectionHandler = onConnectionEvent;
    currentBatchHandler = onEventBatch ?? null;
  },
}));

function pushEvent(ev: SystemEvent): void {
  if (!currentEventHandler) {
    throw new Error("useTimeline not mounted yet, no event handler");
  }
  act(() => {
    currentEventHandler!(ev);
  });
}

function pushBatch(evs: SystemEvent[]): void {
  if (!currentBatchHandler) {
    throw new Error("useTimeline did not pass a batch handler");
  }
  act(() => {
    currentBatchHandler!(evs);
  });
}

function pushConnectionEvent(ev: ConnectionEvent): void {
  if (!currentConnectionHandler) {
    throw new Error("useTimeline did not pass onConnectionEvent");
  }
  act(() => {
    currentConnectionHandler!(ev);
  });
}

// Pristine store actions, captured before any test mutates them. Several tests
// swap store actions for spies via useTimelineStore.setState({ ... }) and never restore
// them (setState overwrites are not vi spies, so restoreAllMocks can't undo
// them). Restoring these in beforeEach keeps a leftover spy from one test out of
// the next — otherwise switchThread's stable-reference contract is broken across
// tests and its effect re-fires spuriously.
const REAL_SWITCH_THREAD = useTimelineStore.getState().switchThread;
const REAL_RELOAD_SNAPSHOT = useTimelineStore.getState().reloadSnapshot;

// -- helpers ───────────────────────────────────────────────────────────────


let _id_counter_ut = 0;
function snapshotItem(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `ut.${++_id_counter_ut}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

// Wrap items into the GET /timeline response shape. msg_count is computed the
// same way the old inferMsgCount did (max parseable msg_idx + 1; the ut.N test
// ids are unparseable → 0), so reloadSnapshot sees identical (items, msg_count)
// and these tests keep their original semantics.
function postWindow(): BackendTimelineItem[] {
  return [
    snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
    snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
    snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "post-compact" }),
  ];
}

function tlResp(items: BackendTimelineItem[] = [], has_more = false): TimelineResponse {
  let max = -1;
  for (const it of items) {
    const parsed = parseItemId(it.item_id);
    if (parsed && parsed[0] > max) max = parsed[0];
  }
  return { items, msg_count: max + 1, has_more };
}

// The composed switch-refresh payload (agent-reconcile.ts) around a timeline
// window; token/pending sections stay empty where a test doesn't read them.
function composedSnapshot(timeline: TimelineResponse): ConversationSnapshotResponse {
  return {
    timeline,
    token_usage: { input_tokens: 0, output_tokens: 0, reasoning_tokens: 0,
      max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0 },
    pending: [],
  };
}

function resolves<T>(value: T): () => Promise<T> {
  return () => Promise.resolve(value);
}

function rejects(error: Error): () => Promise<never> {
  return () => Promise.reject(error);
}

// meiva:note
// The test gateway: every timeline and snapshot read is logged, then answered by
// the next queued answer or, once those are used, by the standing one. The user
// settings and the display config the hook reads through its own query keys are
// seeded into the cache instead (seedSettings below), so no read reaches a network.
const timelineReads: TimelineRead[] = [];
const snapshotReads: SnapshotRead[] = [];
const turnStarts: number[] = [];
type TimelineAnswer = Answer<[agentId: number, options: TimelineReadOptions | undefined], TimelineResponse>;
type SnapshotAnswer = Answer<[agentId: number, signal: AbortSignal | undefined], ConversationSnapshotResponse>;
let queuedTimelineAnswers: TimelineAnswer[] = [];
let standingTimelineAnswer: TimelineAnswer = resolves(tlResp([]));
let queuedSnapshotAnswers: SnapshotAnswer[] = [];
let standingSnapshotAnswer: SnapshotAnswer = resolves(composedSnapshot(tlResp([])));

const Gateway = apiWrapper(fakeApi({
  getTimeline: (agentId, options) => {
    timelineReads.push({
      agentId,
      before: options?.before,
      limit: options?.limit,
      signal: options?.signal,
    });
    return (queuedTimelineAnswers.shift() ?? standingTimelineAnswer)(agentId, options);
  },
  getConversationSnapshot: (agentId, signal) => {
    snapshotReads.push({ agentId, signal });
    return (queuedSnapshotAnswers.shift() ?? standingSnapshotAnswer)(agentId, signal);
  },
}));

let queryClient: QueryClient;
let visibility: DocumentVisibilityState = "visible";

beforeEach(() => {
  startTransitionSpy.mockClear();
  timelineReads.length = 0;
  snapshotReads.length = 0;
  turnStarts.length = 0;
  queuedTimelineAnswers = [];
  standingTimelineAnswer = resolves(tlResp([]));
  queuedSnapshotAnswers = [];
  standingSnapshotAnswer = resolves(composedSnapshot(tlResp([])));
  interactionTimingResetForTest((agentId) => { turnStarts.push(agentId); });
  currentEventHandler = null;
  currentConnectionHandler = null;
  currentBatchHandler = null;
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  useTimelineStore.setState({ switchThread: REAL_SWITCH_THREAD, reloadSnapshot: REAL_RELOAD_SNAPSHOT });
  useTimelineStore.getState().switchThread(0, null, false);
  useTimelineStore.setState({ compactReplaceSeq: 0, compactReplaceAgent: null });
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity },
    },
  });
  queryClient.setQueryData(["config", null], { fields: [] });
  seedSettings({});
});

afterEach(() => {
  cleanup();
  interactionTimingResetForTest();
  Reflect.deleteProperty(document, "visibilityState");
  currentEventHandler = null;
  currentConnectionHandler = null;
  currentBatchHandler = null;
});

function seedSettings(overrides: Record<string, unknown>): void {
  queryClient.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...overrides });
}

function setVisibility(next: DocumentVisibilityState): void {
  visibility = next;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(
    QueryClientProvider,
    { client: queryClient },
    React.createElement(Gateway, null, children),
  );
}

// -- tests ─────────────────────────────────────────────────────────────────




describe("useTimeline mount + initial fetch", () => {
  it("on mount, immediately fetches GET /timeline", async () => {
    const showError = vi.fn();
    standingTimelineAnswer = resolves(tlResp([
      snapshotItem({ kind: "agent_chat", payload: "hi" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(1);
    });
    expect(timelineReads[0]).toMatchObject({ agentId: 42, signal: expect.any(AbortSignal) as AbortSignal });
    expect(result.current.items[0].payload).toBe("hi");
    expect(showError).not.toHaveBeenCalled();
  });

  it("hasMoreOlder reflects the page; loadOlder fetches before=oldest id and prepends", async () => {
    const showError = vi.fn();
    queuedTimelineAnswers = [
      resolves(tlResp(
        [
          snapshotItem({ item_id: "5.0", kind: "inbound_chat", payload: "recent" }),
          snapshotItem({ item_id: "6.0", payload: "newest" }),
        ],
        true,
      )),
      resolves(tlResp([snapshotItem({ item_id: "3.0", kind: "inbound_chat", payload: "older" })], false)),
    ];

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(result.current.hasMoreOlder).toBe(true);

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(timelineReads.at(-1)).toEqual({ agentId: 42, before: "5.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    expect(result.current.items.map((i) => i.item_id)).toEqual(["3.0", "5.0", "6.0"]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(showError).not.toHaveBeenCalled();
  });

  it("loadOlder skips all re-attached context and keeps paging from the oldest real item", async () => {
    // GET /timeline re-attaches the prompt and compact summary at every
    // window head. Neither is a valid cursor for the historical tail.
    const showError = vi.fn();
    queuedTimelineAnswers = [
      resolves(tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "960.1", kind: "inbound_chat", payload: "recent" }),
        ],
        true,
      )),
      resolves(tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "915.1", kind: "agent_chat", payload: "older" }),
        ],
        true,
      )),
      resolves(tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "800.1", kind: "agent_chat", payload: "oldest" }),
        ],
        false,
      )),
    ];

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(result.current.hasMoreOlder).toBe(true);
    timelineReads.length = 0;

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(4));
    expect(timelineReads[0]).toEqual({
      agentId: 42,
      before: "960.1",
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "6.0",
      "915.1",
      "960.1",
    ]);
    expect(result.current.hasMoreOlder).toBe(true);

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(5));
    expect(timelineReads[1]).toEqual({
      agentId: 42,
      before: "915.1",
      limit: 100,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "6.0",
      "800.1",
      "915.1",
      "960.1",
    ]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(showError).not.toHaveBeenCalled();
  });

});

const testContext: UseTimelineTestContext = {
  ApiError,
  timeline: {
    queue: (...answers) => { queuedTimelineAnswers.push(...answers); },
    always: (answer) => { standingTimelineAnswer = answer; },
  },
  snapshot: {
    queue: (...answers) => { queuedSnapshotAnswers.push(...answers); },
    always: (answer) => { standingSnapshotAnswer = answer; },
  },
  timelineReads,
  snapshotReads,
  turnStarts,
  resolves,
  rejects,
  startTransitionSpy,
  pushEvent,
  pushBatch,
  pushConnectionEvent,
  snapshotItem,
  postWindow,
  tlResp,
  composedSnapshot,
  getQueryClient: () => queryClient,
  wrapper,
  seedSettings,
  setVisibility,
  delay: (ms = 0) => new Promise((r) => setTimeout(r, ms)),
};

registerUseTimelinePaginationTests(testContext);
