// useEventStream unit tests — mock global EventSource and verify
// readyState tri-state dispatch + onmessage parse path directly. This
// is the only load-bearing new logic from PR #18 (silent-failure-hunter
// F7+F8 fixes); the entire useTimeline connectionState behavior
// depends on this file's onerror branches being correct, so they must
// be tested directly.
//
// 2026-05-06 refactor: useEventStream changed from (agentId, onSystem,
// onConn) to a Context Provider pattern (agentId on EventStreamProvider).
// Tests use renderHook's `wrapper` option to wrap each hook call in a
// Provider.

const mocks = vi.hoisted(() => ({ checkAuth: vi.fn() }));
vi.mock("@/lib/transport/api", () => ({ API_BASE: "", api: { checkAuth: mocks.checkAuth } }));

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "../state/store";
import { AuthProvider, useAuth } from "../auth/auth-context";
import { AGENTS_QUERY_KEY, RECONNECT_QUERY_KEYS } from "../fold";
import { useFoldOwner } from "../fold/owner";
import { registerAgentEventStreamTests } from "./tests/use-event-stream-agent";
import type { EventStreamTestContext } from "./tests/use-event-stream-context";
import { registerEventStreamProviderTests } from "./tests/use-event-stream-provider";
import type { ConnectionEvent } from "./useEventStream";
import {
  AgentEventStreamProvider,
  EventStreamProvider,
  useEventStream,
} from "./useEventStream";

// -- mock global EventSource ----------------------------------------

class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  url: string;
  init?: EventSourceInit;
  readyState = MockEventSource.CONNECTING;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;

  constructor(url: string, init?: EventSourceInit) {
    this.url = url;
    this.init = init;
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- mock must track the latest constructed instance
    lastInstance = this;
    instances.push(this);
  }

  close(): void {
    this.readyState = MockEventSource.CLOSED;
  }

  // Test-driver helper — set readyState + trigger onerror directly, not via EventSource API
  fireOpen(): void {
    this.readyState = MockEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }
  fireMessage(data: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: data as string }));
  }
  fireErrorWithReadyState(state: number): void {
    this.readyState = state;
    this.onerror?.(new Event("error"));
  }
}

let lastInstance: MockEventSource | null = null;
let instances: MockEventSource[] = [];

function deferredAuthResult() {
  let resolve!: (result: { authenticated: boolean }) => void;
  const promise = new Promise<{ authenticated: boolean }>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  lastInstance = null;
  instances = [];
  mocks.checkAuth.mockReset();
  mocks.checkAuth.mockResolvedValue({ authenticated: true });
  navigator.sendBeacon = () => true;
  vi.stubGlobal("EventSource", MockEventSource);
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  useStore.setState({ reconnectNonce: 0, activeId: null });

});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function expectInstance(): MockEventSource {
  if (!lastInstance) throw new Error("EventSource was not constructed");
  return lastInstance;
}

async function waitForInstance(): Promise<void> {
  await waitFor(() => expect(lastInstance).not.toBeNull());
}

// Helps renderHook wrap a hook into the Provider. agentId is passed via the wrapper factory.
function withProvider() {
  // The R4 fold owner inside EventStreamProvider reads the query cache, so
  // the Provider must sit under a QueryClientProvider (mirrors providers.tsx).
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      AuthProvider,
      null,
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(EventStreamProvider, { children }),
      ),
    );
  };
}

// Variant of withProvider that also hands back the QueryClient, so a test can
// seed and assert the fold's cache writes.
function withProviderAndClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Plain arrow function (not an object method) — destructuring it out of the
  // return value would otherwise trip @typescript-eslint/unbound-method.
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(
      AuthProvider,
      null,
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(EventStreamProvider, { children }),
      ),
    );
  return { qc, wrapper };
}


// -- tests ─────────────────────────────────────────────────────────────────

describe("EventStreamProvider connection lifecycle", () => {
  it("without profile APIs, separate pages keep their own EventSources", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitFor(() => expect(instances).toHaveLength(2));
    expect(instances.map((source) => source.url)).toEqual(["/api/system", "/api/system"]);
  });

  it("mount + open → subscriber receives onConnectionEvent({type:'open'})", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => expectInstance().fireOpen());

    expect(onConn).toHaveBeenCalledWith({ type: "open" });
    expect(onSystem).not.toHaveBeenCalled();
  });

  it("provider always opens EventSource (global connection)", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    expect(lastInstance).not.toBeNull();
    expect(expectInstance().url).toContain("/api/system");
  });

  it("URL is /api/system (global broadcast)", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    expect(expectInstance().url).toContain("/api/system");
  });

  it("hidden tab closes the global stream and reopens it when visible", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    const first = expectInstance();

    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(first.readyState).toBe(MockEventSource.CLOSED);

    act(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(expectInstance()).not.toBe(first);
    expect(expectInstance().url).toContain("/api/system");
  });

  it("opens with withCredentials so the session cookie reaches the cross-origin gateway", async () => {
    // Regression: the gateway requires auth and EventSource cannot send a
    // Bearer header, so a cross-origin SSE GET (:3000 -> :8000) must carry the
    // session cookie via withCredentials — otherwise it 401s and live updates
    // never connect ("SSE connection lost").
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    expect(expectInstance().init?.withCredentials).toBe(true);
  });

  it("calling useEventStream outside Provider → throws (fail-fast, not silent)", () => {
    // Suppress React error-boundary console.error noise
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined /* suppress console.error in error-boundary test */);
    expect(() => renderHook(() => useEventStream(vi.fn(), vi.fn()))).toThrow(
      /EventStreamProvider/,
    );
    errSpy.mockRestore();
  });
});

describe("EventStreamProvider onmessage", () => {
  it("valid JSON → onSystemEvent receives the parsed event", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify({ role: "chat_delta", agent_id: 42, content: "hi" }),
      );
    });

    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 42,
      content: "hi",
    });
  });

  it("malformed JSON → onConnectionEvent({type:'parse-failed'}) (no more silent console.error)", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage("{not valid json");
    });

    expect(onSystem).not.toHaveBeenCalled();
    expect(onConn).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "parse-failed",
        raw: "{not valid json",
        // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- expect.any() returns any, required by Vitest API
        error: expect.any(SyntaxError),
      }),
    );
  });

  it("non-string e.data (Blob etc.) → 'parse-failed', does not let String() itself crash", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage({ type: "Blob", size: 100 });
    });

    expect(onConn).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "parse-failed",
        raw: "[non-string SSE payload]",
      }),
    );
  });
});


describe("EventStreamProvider half-dead watchdog", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shared CONNECTING state keeps the watchdog armed until a frame arrives", async () => {
    const locks = {
      request: (_name: string, _options: LockOptions, callback: (lock: Lock) => Promise<void>) =>
        callback({ name: "meiva-ui-sse", mode: "exclusive" }),
    };
    const nativeNavigator = navigator;
    vi.stubGlobal("navigator", new Proxy(nativeNavigator, {
      get(target, key): unknown {
        if (key === "locks") return locks;
        const value: unknown = Reflect.get(target, key, target);
        return value;
      },
    }));
    vi.stubGlobal("BroadcastChannel", class {
      onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
      postMessage(_message: unknown): void { /* one-tab test */ }
      close(): void { /* one-tab test */ }
    });

    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitFor(() => expect(instances).toHaveLength(2));
    const system = instances.find((source) => source.url === "/api/system");
    if (!system) throw new Error("system source missing");
    vi.useFakeTimers();
    act(() => {
      system.fireOpen();
      system.fireErrorWithReadyState(MockEventSource.CONNECTING);
      vi.advanceTimersByTime(45_000);
    });

    expect(system.readyState).toBe(MockEventSource.CLOSED);
    expect(instances.filter((source) => source.url === "/api/system")).toHaveLength(2);
  });

  it("45s with NO frame → notifies reconnecting + bumps reconnectNonce", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    act(() => expectInstance().fireOpen());
    onConn.mockClear();

    const nonceBefore = useStore.getState().reconnectNonce;
    act(() => {
      vi.advanceTimersByTime(45_000);
    });

    expect(onConn).toHaveBeenCalledWith({ type: "reconnecting" });
    expect(useStore.getState().reconnectNonce).toBe(nonceBefore + 1);
  });

  it("any frame (incl. heartbeat) resets the watchdog — no fire before 45s of silence", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    act(() => expectInstance().fireOpen());
    onConn.mockClear();
    const nonceBefore = useStore.getState().reconnectNonce;

    // Advance 30s, deliver a heartbeat (resets), advance another 30s.
    // Total elapsed 60s but never 45s of continuous silence → no fire.
    act(() => {
      vi.advanceTimersByTime(30_000);
      expectInstance().fireMessage(JSON.stringify({ role: "heartbeat" }));
      vi.advanceTimersByTime(30_000);
    });

    expect(onConn).not.toHaveBeenCalledWith({ type: "reconnecting" });
    expect(useStore.getState().reconnectNonce).toBe(nonceBefore);
  });
});

describe("fold subscription lifecycle (Task #1033 regression)", () => {
  it("fold subscription survives the Provider re-render that SSE onopen triggers", async () => {
    const { qc, wrapper } = withProviderAndClient();
    // Seed the agents cache so the fold's empty-cache guard accepts the merge.
    qc.setQueryData(AGENTS_QUERY_KEY, { agents: [], ancestors: [] });
    renderHook(() => useEventStream(() => undefined, () => undefined), { wrapper });
    await waitForInstance();

    // First open: onOpenChange(true) → setSseOpen(true) → Provider re-render.
    // Before the fix that re-render ran the fold effect's cleanup
    // (unsubscribe) and the foldSubscribedRef early-return skipped the
    // resubscribe — the fold was permanently gone from the subscriber set
    // and every domain's realtime layer went silently stale.
    act(() => expectInstance().fireOpen());

    // A later business event must still reach the fold and land in the cache.
    // (fireMessage mirrors the wire format: JSON string — a non-string payload
    // goes down the parse-failed path and never reaches subscribers.)
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify({
          role: "agent_spawned",
          agent_id: 2,
        }),
      );
    });

    await waitFor(() => expect(qc.getQueryState(AGENTS_QUERY_KEY)?.isInvalidated).toBe(true));
    expect(qc.getQueryData(AGENTS_QUERY_KEY)).toEqual({ agents: [], ancestors: [] });
  });

  it("useFoldOwner returns a stable reference across re-renders", () => {
    const { result, rerender } = renderHook(() => useFoldOwner(), {
      wrapper: withProvider(),
    });
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it("every reconnect gap repairs scoped queries, including a second gap within 30 seconds", async () => {
    const { qc, wrapper } = withProviderAndClient();
    for (const key of RECONNECT_QUERY_KEYS) qc.setQueryData(key, []);
    const spy = vi.spyOn(qc, "invalidateQueries");
    renderHook(() => useEventStream(() => undefined, () => undefined), { wrapper });
    await waitForInstance();
    await act(async () => { expectInstance().fireOpen(); await Promise.resolve(); });
    expect(spy).toHaveBeenCalledTimes(RECONNECT_QUERY_KEYS.length);
    spy.mockClear();
    await act(async () => { expectInstance().fireOpen(); await Promise.resolve(); });
    expect(spy).toHaveBeenCalledTimes(RECONNECT_QUERY_KEYS.length);
  });
});

function withAgentProvider() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      AuthProvider,
      null,
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(AgentEventStreamProvider, { children }),
      ),
    );
  };
}

const eventStreamTestContext: EventStreamTestContext = {
  checkAuthMock: mocks.checkAuth,
  MockEventSource,
  getLastInstance: () => lastInstance,
  getInstances: () => instances,
  resetInstances: () => {
    lastInstance = null;
    instances = [];
  },
  deferredAuthResult,
  expectInstance,
  waitForInstance,
  withProvider,
  withAgentProvider,
  useAuth,
  getStoreState: () => useStore.getState(),
  setStoreState: (partial) => useStore.setState(partial),
  setDocumentVisibility: (state) => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: state,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  },
  spyConsoleError: () => vi.spyOn(console, "error"),
};

registerEventStreamProviderTests(eventStreamTestContext);
registerAgentEventStreamTests(eventStreamTestContext);
