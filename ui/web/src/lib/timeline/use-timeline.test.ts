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
import { ApiProvider } from "../transport/transport-context";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig, httpRig, noUpload, type HttpRig } from "../transport/tests/api/api-provider";
import { realNetwork } from "../transport/tests/api/api-provider";
import { UncancellableFetch } from "../transport/tests/api/api-provider";
import { systemKeySource } from "../transport/system-transport";
import { interactionTimingResetForTest } from "../format/interaction-timing";
import { SETTINGS_QUERY_KEY } from "../state/use-user-settings";
import type {
  Answer,
  SnapshotRead,
  TimelineRead,
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

function resolves(value: unknown): GatewayReply {
  return { kind: "json", body: value };
}

function rejects(error: Error): GatewayReply {
  return { kind: "json", status: error instanceof ApiError ? error.status : 503, body: { detail: error.message } };
}

// meiva:note
// These are HTTP response values, including held responses. Endpoint paths and
// cursors are observed at the real server; only AbortSignal lives on the client.
const timelineReads: TimelineRead[] = [];
const snapshotReads: SnapshotRead[] = [];
const turnStarts: number[] = [];
const wireReplies = new Map<string, Answer[]>();
let queuedTimelineAnswers: Answer[] = [];
let standingTimelineAnswer: Answer = resolves(tlResp([]));
let queuedSnapshotAnswers: Answer[] = [];
let standingSnapshotAnswer: Answer = resolves(composedSnapshot(tlResp([])));
let gateway: TestGateway;
let rig: HttpRig;

function Gateway({ children }: { children: React.ReactNode }) {
  return React.createElement(ApiProvider, { api: rig.api, children });
}

let queryClient: QueryClient;
let visibility: DocumentVisibilityState = "visible";

beforeEach(async () => {
  wireReplies.clear();
  gateway = await startGateway((request) => {
    const url = new URL(request.path, gateway.origin);
    const match = /^\/api\/agents\/(\d+)\/(timeline|conversation-snapshot)$/.exec(url.pathname);
    if (match === null) return { kind: "text", status: 404, body: "unexpected route" };
    const signal = rig.sent.findLast((sent) => sent.url === `${gateway.origin}${request.path}`)?.init.signal;
    if (signal == null) throw new Error("the native request has no observed abort signal");
    // meiva:note
    // Limit remains observable below; canned pages are selected by their wire cursor.
    const route = new URL(url);
    route.searchParams.delete("limit");
    const scripted = wireReplies.get(`${route.pathname}${route.search}`)?.shift();
    if (match[2] === "timeline") {
      const limit = url.searchParams.get("limit");
      timelineReads.push({ agentId: Number(match[1]), before: url.searchParams.get("before") ?? undefined,
        limit: limit === null ? undefined : Number(limit), signal });
      return scripted ?? queuedTimelineAnswers.shift() ?? standingTimelineAnswer;
    }
    snapshotReads.push({ agentId: Number(match[1]), signal });
    return scripted ?? queuedSnapshotAnswers.shift() ?? standingSnapshotAnswer;
  });
  rig = gatewayRig(gateway);
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

afterEach(async () => {
  cleanup();
  interactionTimingResetForTest();
  queryClient.clear();
  await gateway.close();
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

// meiva:note
// Native abort tests above cannot deliver their old payload after cancellation.
// These faults exercise the hook's independent ownership checks while preserving
// the real API factory/HTTP decoder and using only its existing fetch seam.
describe("timeline ownership when a transport ignores abort", () => {
  it("rejects the first A payload after A-to-B-to-A has selected the newer A", async () => {
    const faults = Array.from({ length: 3 }, () => new UncancellableFetch());
    let next = 0;
    rig = httpRig({ base: gateway.origin, fetch: (url, init) => faults[next++].fetch(url, init),
      now: () => performance.now(), keys: systemKeySource(), openUpload: noUpload });
    try {
      const view = renderHook(({ id }: { id: number }) => useTimeline(id, vi.fn()), {
        initialProps: { id: 1 }, wrapper,
      });
      await waitFor(() => expect(rig.sent).toHaveLength(1));
      view.rerender({ id: 2 });
      await waitFor(() => expect(rig.sent).toHaveLength(2));
      expect(rig.sent[0].init.signal?.aborted).toBe(true);
      view.rerender({ id: 1 });
      await waitFor(() => expect(rig.sent).toHaveLength(3));
      expect(rig.sent[1].init.signal?.aborted).toBe(true);
      await act(async () => { faults[2].respond(tlResp([snapshotItem({ item_id: "3.0", payload: "current A" })])); await Promise.resolve(); });
      await waitFor(() => expect(view.result.current.items[0]?.payload).toBe("current A"));
      await act(async () => {
        faults[0].respond(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete A" })]));
        faults[1].respond(tlResp([snapshotItem({ item_id: "2.0", payload: "obsolete B" })]));
        await Promise.resolve();
      });
      expect(rig.responses).toHaveLength(3);
      expect(view.result.current.items.map((item) => item.payload)).toEqual(["current A"]);
    } finally {
      for (const fault of faults) fault.respond(tlResp([]));
    }
  });

  it.each(["selection", "hidden", "compact", "unmount"])("%s rejects history decoded after its cancellation", async (cause) => {
    const fault = new UncancellableFetch();
    standingTimelineAnswer = resolves(tlResp([snapshotItem({ item_id: "5.0", payload: "current" })], true));
    seedSettings({ "display.compact_history_sessions": 0 });
    rig = httpRig({ base: gateway.origin,
      fetch: (url, init) => {
        if (typeof url !== "string") throw new Error("the gateway factory must send a URL string");
        return new URL(url).searchParams.has("before") ? fault.fetch(url, init) : realNetwork.fetch(url, init);
      },
      now: () => performance.now(), keys: systemKeySource(), openUpload: noUpload });
    const showError = vi.fn();
    try {
      const view = renderHook(({ id }: { id: number }) => useTimeline(id, showError), {
        initialProps: { id: 1 }, wrapper,
      });
      await waitFor(() => expect(view.result.current.hasMoreOlder).toBe(true));
      act(() => view.result.current.loadOlder());
      await waitFor(() => expect(rig.sent.filter((sent) => new URL(sent.url).searchParams.has("before"))).toHaveLength(1));
      const history = rig.sent.find((sent) => new URL(sent.url).searchParams.has("before"));
      if (cause === "selection") {
        view.rerender({ id: 2 });
        await waitFor(() => expect(gateway.requests.some((request) => request.path === "/api/agents/2/timeline")).toBe(true));
        view.rerender({ id: 1 });
        await waitFor(() => expect(useTimelineStore.getState().activeThreadId).toBe(1));
      } else if (cause === "hidden") {
        setVisibility("hidden");
      } else if (cause === "unmount") {
        view.unmount();
      } else {
        pushEvent({ role: "compact_done", agent_id: 1 });
      }
      expect(history?.init.signal?.aborted).toBe(true);
      await act(async () => { fault.respond(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete history" })])); await Promise.resolve(); });
      expect(rig.responses.length).toBeGreaterThanOrEqual(2);
      expect(useTimelineStore.getState().items.some((item) => item.payload === "obsolete history")).toBe(false);
      expect(showError).not.toHaveBeenCalled();
    } finally {
      fault.respond(tlResp([]));
    }
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
  wireReplies,
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
