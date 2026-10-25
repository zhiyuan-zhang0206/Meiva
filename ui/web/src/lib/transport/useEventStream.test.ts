// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, createElement, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { create } from "zustand";

import { AGENTS_QUERY_KEY, RECONNECT_QUERY_KEYS } from "../fold";
import { useFoldOwner } from "../fold/owner";
import {
  notifySessionInvalid,
  registerSseRuntime,
  setSessionInvalidHandler,
  type SseAuthStatus,
  type SseStoreSlice,
} from "./sse-lifecycle";
import { StreamHubProvider } from "./transport-context";
import { StreamHub } from "./stream/stream-hub";
import { FakeNetwork, FakeTab, READY, type FakeEventSource } from "./stream/tests/fake-stream";
import { ScriptedAuth } from "./stream/tests/scripted-auth";
import { registerAgentEventStreamTests } from "./tests/suite/use-event-stream-agent";
import type { EventStreamTestContext } from "./tests/suite/use-event-stream-context";
import { registerEventStreamProviderTests } from "./tests/suite/use-event-stream-provider";
import type { ConnectionEvent } from "./useEventStream";
import {
  AgentEventStreamProvider,
  EventStreamProvider,
  useEventStream,
} from "./useEventStream";

const useStore = create<SseStoreSlice>()((set) => ({
  reconnectNonce: 0,
  bumpReconnect: () => set((s) => ({ reconnectNonce: s.reconnectNonce + 1 })),
  activeId: null,
}));

const AuthContext = createContext<{ status: SseAuthStatus } | null>(null);

function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SseAuthStatus>("loading");
  useEffect(() => {
    setSessionInvalidHandler(() => setStatus("unauthenticated"));
    return () => setSessionInvalidHandler(null);
  }, []);
  useEffect(() => {
    let cancelled = false;
    auth
      .checkAuth()
      .then((res) => {
        if (!cancelled) setStatus(res.authenticated ? "authenticated" : "unauthenticated");
      })
      .catch(() => {
        if (!cancelled) setStatus("unauthenticated");
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return createElement(AuthContext.Provider, { value: { status } }, children);
}

function useAuth(): { status: SseAuthStatus } {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

function useDocumentVisible(): boolean {
  const [isVisible, setIsVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );
  useEffect(() => {
    const sync = () => setIsVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", sync);
    sync();
    return () => document.removeEventListener("visibilitychange", sync);
  }, []);
  return isVisible;
}

// meiva:note
// -- the page's transport: a fake tab whose event sources the test drives ----

let auth: ScriptedAuth;
let network: FakeNetwork;
let tab: FakeTab;
let hub: StreamHub;

function useTransport(capability: "legacy" | "shared"): void {
  network = new FakeNetwork();
  tab = new FakeTab({ network, capability, wallNow: () => Date.now(), secureContext: true });
  hub = new StreamHub(tab.transport, { checkAuth: auth.checkAuth, sessionInvalid: notifySessionInvalid }, "");
}

function lastInstance(): FakeEventSource | null {
  return tab.sources.at(-1) ?? null;
}

beforeEach(() => {
  auth = new ScriptedAuth();
  useTransport("legacy");
  navigator.sendBeacon = () => true;
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  useStore.setState({ reconnectNonce: 0, activeId: null });
  registerSseRuntime({
    useAuthStatus: () => useAuth().status,
    useStore,
    useDocumentVisible,
  });
});

afterEach(() => {
  cleanup();
});

function expectInstance(): FakeEventSource {
  const source = lastInstance();
  if (!source) throw new Error("EventSource was not constructed");
  return source;
}

async function waitForInstance(): Promise<void> {
  await waitFor(() => expect(lastInstance()).not.toBeNull());
}

// Helps renderHook wrap a hook into the Provider. agentId is passed via the wrapper factory.
function withProvider() {
  // The R4 fold owner inside EventStreamProvider reads the query cache, so
  // the Provider must sit under a QueryClientProvider (mirrors providers.tsx).
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(StreamHubProvider, {
      hub,
      children: createElement(
        AuthProvider,
        null,
        createElement(
          QueryClientProvider,
          { client: qc },
          createElement(EventStreamProvider, { children }),
        ),
      ),
    });
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
    createElement(StreamHubProvider, {
      hub,
      children: createElement(
        AuthProvider,
        null,
        createElement(
          QueryClientProvider,
          { client: qc },
          createElement(EventStreamProvider, { children }),
        ),
      ),
    });
  return { qc, wrapper };
}


// -- tests ─────────────────────────────────────────────────────────────────

describe("EventStreamProvider connection lifecycle", () => {
  it("without profile APIs, separate pages keep their own EventSources", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitFor(() => expect(tab.sources).toHaveLength(2));
    expect(tab.sources.map((source) => source.url)).toEqual(["/api/system", "/api/system"]);
  });

  it("mount + open → subscriber receives onConnectionEvent({type:'open'})", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => expectInstance().open());

    expect(onConn).toHaveBeenCalledWith({ type: "open" });
    expect(onSystem).not.toHaveBeenCalled();
  });

  it("provider always opens EventSource (global connection)", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    expect(lastInstance()).not.toBeNull();
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
    expect(first.readyState).toBe(READY.CLOSED);

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
    expect(expectInstance().init.withCredentials).toBe(true);
  });

  it("calling useEventStream outside Provider → throws (fail-fast, not silent)", () => {
    // Suppress React error-boundary console.error noise
    expect(() => renderHook(() => useEventStream(vi.fn(), vi.fn()))).toThrow(
      /EventStreamProvider/,
    );
  });
});

describe("EventStreamProvider onmessage", () => {
  it("valid JSON → onSystemEvent receives the parsed event", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().open();
      expectInstance().frame(
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
      expectInstance().open();
      expectInstance().frame("{not valid json");
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
      expectInstance().open();
      expectInstance().frame({ type: "Blob", size: 100 });
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
    useTransport("shared");
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitFor(() => expect(tab.sources).toHaveLength(2));
    const system = tab.sources.find((source) => source.url === "/api/system");
    if (!system) throw new Error("system source missing");
    vi.useFakeTimers();
    act(() => {
      system.open();
      system.error(READY.CONNECTING);
      vi.advanceTimersByTime(45_000);
    });

    expect(system.readyState).toBe(READY.CLOSED);
    expect(tab.sources.filter((source) => source.url === "/api/system")).toHaveLength(2);
  });

  it("45s with NO frame → notifies reconnecting + bumps reconnectNonce", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    act(() => expectInstance().open());
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
    act(() => expectInstance().open());
    onConn.mockClear();
    const nonceBefore = useStore.getState().reconnectNonce;

    // Advance 30s, deliver a heartbeat (resets), advance another 30s.
    // Total elapsed 60s but never 45s of continuous silence → no fire.
    act(() => {
      vi.advanceTimersByTime(30_000);
      expectInstance().frame(JSON.stringify({ role: "heartbeat" }));
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
    act(() => expectInstance().open());

    // A later business event must still reach the fold and land in the cache.
    // (fireMessage mirrors the wire format: JSON string — a non-string payload
    // goes down the parse-failed path and never reaches subscribers.)
    act(() => {
      expectInstance().frame(
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
    const seed = () => { for (const key of RECONNECT_QUERY_KEYS) qc.setQueryData(key, []); };
    const invalidated = () => RECONNECT_QUERY_KEYS.map((key) => qc.getQueryState(key)?.isInvalidated);
    seed();
    expect(invalidated()).toEqual(RECONNECT_QUERY_KEYS.map(() => false));
    renderHook(() => useEventStream(() => undefined, () => undefined), { wrapper });
    await waitForInstance();
    await act(async () => { expectInstance().open(); await Promise.resolve(); });
    expect(invalidated()).toEqual(RECONNECT_QUERY_KEYS.map(() => true));
    seed();
    await act(async () => { expectInstance().open(); await Promise.resolve(); });
    expect(invalidated()).toEqual(RECONNECT_QUERY_KEYS.map(() => true));
  });
});

function withAgentProvider() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(StreamHubProvider, {
      hub,
      children: createElement(
        AuthProvider,
        null,
        createElement(
          QueryClientProvider,
          { client: qc },
          createElement(AgentEventStreamProvider, { children }),
        ),
      ),
    });
  };
}

const eventStreamTestContext: EventStreamTestContext = {
  auth: {
    always: (result) => auth.always(result),
    once: (result) => auth.once(result),
    onceDeferred: () => auth.onceDeferred(),
    probes: () => auth.probes,
  },
  READY,
  getLastInstance: lastInstance,
  getInstances: () => tab.sources,
  resetInstances: () => {
    tab.sources.length = 0;
  },
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
  reportedErrors: () => tab.errors,
};

registerEventStreamProviderTests(eventStreamTestContext);
registerAgentEventStreamTests(eventStreamTestContext);
