// SSE notifications reconcile authoritative snapshots, including capped pages.

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, notifySessionInvalid, useAuth } from "../auth/auth-context";
import { BrowserProvider } from "../browser/browser-provider";
import { fakeBrowser } from "../browser/testing";
import {
  ALERTS_QUERY_KEY,
  ALERTS_SECTION_QUERY_KEY,
  AlertsProvider,
  useAlerts,
  useAlertsSection,
} from "./use-alerts";
import type { Alert, AlertsResponse } from "../contracts/types";
import type { Api } from "../transport/api";
import { StreamHub } from "../transport/stream/stream-hub";
import { realNetwork, startGateway, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig, type HttpRig } from "../transport/tests/api/api-provider";
import { FakeNetwork, FakeTab, READY, type FakeEventSource } from "../transport/stream/tests/fake-stream";
import { ScriptedAuth } from "../transport/stream/tests/scripted-auth";
import { ApiProvider, StreamHubProvider } from "../transport/transport-context";

function row(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 1,
    status: "unresolved",
    severity: "error",
    alertname: "r",
    labels: {},
    annotations: {},
    starts_at: "2026-08-12T20:00:00Z",
    ends_at: null,
    fingerprint: "f",
    generator_url: "",
    source: "grafana",
    notified_at: null,
    created_at: "2026-08-12T20:00:00Z",
    updated_at: "2026-08-12T20:00:00Z",
    ...overrides,
  };
}

function resp(alerts: Alert[], unresolved: number): AlertsResponse {
  return {
    alerts,
    meta: { window: "24h", total: alerts.length, unresolved_count: unresolved },
  };
}

let auth: ScriptedAuth;
let gateway: TestGateway;
let rig: HttpRig;
let snapshot: AlertsResponse;

let tab: FakeTab;
let hub: StreamHub;
let queryClients: QueryClient[] = [];

function installTab(capability: "legacy" | "shared"): void {
  tab = new FakeTab({
    network: new FakeNetwork(),
    capability,
    wallNow: () => Date.now(),
    secureContext: true,
  });
  hub = new StreamHub(
    tab.transport,
    { checkAuth: auth.checkAuth, sessionInvalid: notifySessionInvalid },
    "",
  );
}

function defineVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
}

function setVisibility(state: DocumentVisibilityState): void {
  defineVisibility(state);
  document.dispatchEvent(new Event("visibilitychange"));
}

function newQueryClient(): QueryClient {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  queryClients.push(queryClient);
  return queryClient;
}

function readerWrapper() {
  const queryClient = newQueryClient();
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <ApiProvider api={rig.api}>{children}</ApiProvider>
      </QueryClientProvider>
    );
  };
}

function alertsWrapper() {
  const queryClient = newQueryClient();
  const client: Api = {
    ...rig.api,
    checkAuth: () => auth.checkAuth(),
    getSettings: () => Promise.resolve({ settings: [] }),
  };
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      // meiva:note
      // The auth provider reads the page's address through the browser port, so the wrapper
      // mounts the provider the app mounts with a fake page whose address is this test's.
      <BrowserProvider browser={fakeBrowser()}>
        <QueryClientProvider client={queryClient}>
          <ApiProvider api={client}>
            <StreamHubProvider hub={hub}>
              <AuthProvider>
                <AlertsProvider>{children}</AlertsProvider>
              </AuthProvider>
            </StreamHubProvider>
          </ApiProvider>
        </QueryClientProvider>
      </BrowserProvider>
    );
  };
}

function currentSource(): FakeEventSource {
  const source = tab.sources.at(-1);
  if (!source) throw new Error("EventSource was not constructed");
  return source;
}

async function waitForSource(): Promise<void> {
  await waitFor(() => expect(tab.sources.length).toBeGreaterThan(0));
}

function lastQueryClient(): QueryClient {
  const queryClient = queryClients.at(-1);
  if (!queryClient) throw new Error("QueryClient was not constructed");
  return queryClient;
}

beforeEach(async () => {
  queryClients = [];
  auth = new ScriptedAuth();
  snapshot = resp([], 0);
  gateway = await startGateway(realNetwork.createServer, (request) => request.path.split("?", 1)[0] === "/api/alerts"
    ? { kind: "json", body: snapshot }
    : { kind: "text", status: 404, body: "unexpected route" });
  rig = gatewayRig(gateway);
  installTab("legacy");
  defineVisibility("visible");
});

afterEach(async () => {
  cleanup();
  hub.dispose();
  for (const queryClient of queryClients) queryClient.clear();
  vi.useRealTimers();
  defineVisibility("visible");
  await gateway.close();
});

describe("useAlertsSection", () => {
  it("pins the section query to the 24h window named by its empty state", async () => {
    const { result } = renderHook(() => useAlertsSection(), { wrapper: readerWrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/alerts?window=24h&limit=200"]);
  });
});

describe("useAlerts", () => {
  it("fetches the default snapshot when the SSE provider has not warmed the cache", async () => {
    const { result } = renderHook(() => useAlerts(), { wrapper: readerWrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/alerts"]);
  });
});

describe("AlertsProvider connection auth gating", () => {
  it("unauthenticated → no EventSource is constructed for /api/alerts/stream", async () => {
    auth.always({ authenticated: false });

    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("unauthenticated"));
    expect(tab.sources).toHaveLength(0);
  });

  it("reconnect repairs active alert readers and marks inactive caches stale", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const queryClient = lastQueryClient();
    queryClient.setQueryData(ALERTS_SECTION_QUERY_KEY, resp([], 0));
    await waitFor(() => expect(queryClient.getQueryState(ALERTS_QUERY_KEY)?.fetchStatus).toBe("idle"));
    expect(queryClient.getQueryState(ALERTS_SECTION_QUERY_KEY)?.isInvalidated).toBe(false);
    const reads = gateway.requests.length;

    act(() => {
      currentSource().open();
    });

    await waitFor(() => expect(gateway.requests.map((request) => request.path)).toHaveLength(reads + 1));
    await waitFor(() => expect(queryClient.getQueryState(ALERTS_QUERY_KEY)?.fetchStatus).toBe("idle"));
    expect(gateway.requests.at(-1)?.path).toBe("/api/alerts");
    expect(queryClient.getQueryState(ALERTS_SECTION_QUERY_KEY)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(ALERTS_SECTION_QUERY_KEY)?.fetchStatus).toBe("idle");
  });

  it("repeated frames outside the page cannot inflate the uncapped count", async () => {
    snapshot = resp([row({ id: 2 })], 13);
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForSource();
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(13));
    const reads = gateway.requests.length;
    act(() => currentSource().frame(JSON.stringify(row({ id: 999 }))));
    await waitFor(() => expect(gateway.requests.length).toBeGreaterThan(reads));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.meta.unresolved_count).toBe(13);
    expect(result.current.data?.alerts.map((a) => a.id)).toEqual([2]);

    const nextReads = gateway.requests.length;
    act(() => currentSource().frame(JSON.stringify(row({ id: 999 }))));
    await waitFor(() => expect(gateway.requests.length).toBeGreaterThan(nextReads));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.meta.unresolved_count).toBe(13);
  });

  it("a resolution outside the page updates counts and preserves server ordering", async () => {
    snapshot = resp([row({ id: 2 })], 13);
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForSource();
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(13));
    snapshot = resp([row({ id: 2 }), row({ id: 3, status: "resolved" })], 12);
    act(() => currentSource().frame(JSON.stringify(row({ id: 999, status: "resolved" }))));
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(12));
    expect(result.current.data?.alerts.map((a) => a.id)).toEqual([2, 3]);
  });

  it("heartbeats and malformed frames do not fetch alert snapshots", async () => {
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForSource();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const reads = gateway.requests.length;
    act(() => {
      currentSource().frame(JSON.stringify({ role: "heartbeat" }));
      currentSource().frame("bad-json");
      currentSource().frame(JSON.stringify({ id: "invalid" }));
    });
    expect(gateway.requests.map((request) => request.path)).toHaveLength(reads);
  });

  it("CLOSED with an invalid session → auth becomes unauthenticated and never reopens", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();
    vi.useFakeTimers();
    auth.always({ authenticated: false });
    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });

    vi.useRealTimers();
    await waitFor(() => expect(result.current).toBe("unauthenticated"));
    vi.useFakeTimers();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(currentSource()).toBe(first);
  });

  it("CLOSED with a valid session → reopens at the base delay", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();
    vi.useFakeTimers();
    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(currentSource()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(currentSource()).not.toBe(first);
  });

  it("CLOSED when the session probe rejects → reopens at the base delay", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();
    vi.useFakeTimers();
    auth.onceFailing(new Error("gateway unreachable"));
    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(currentSource()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(currentSource()).not.toBe(first);
  });

  it("a late invalid CLOSED probe cannot invalidate a replacement provider", async () => {
    auth.once({ authenticated: true });
    const staleProbe = auth.onceDeferred();
    auth.once({ authenticated: true });

    const firstMount = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(firstMount.result.current).toBe("authenticated"));
    await waitForSource();
    act(() => currentSource().error(READY.CLOSED));
    firstMount.unmount();

    const replacement = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(replacement.result.current).toBe("authenticated"));
    await waitFor(() => expect(tab.sources).toHaveLength(2));
    const replacementSource = currentSource();

    await act(async () => {
      staleProbe.release({ authenticated: false });
      await Promise.resolve();
    });

    expect(replacement.result.current).toBe("authenticated");
    expect(currentSource()).toBe(replacementSource);
    expect(replacementSource.readyState).not.toBe(READY.CLOSED);
  });

  it("consecutive valid-session CLOSED failures use 1s then 2s backoff", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    vi.useFakeTimers();
    const first = currentSource();

    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = currentSource();
    expect(second).not.toBe(first);

    act(() => second.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_999);
    });
    expect(currentSource()).toBe(second);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(currentSource()).not.toBe(second);
  });

  it("a heartbeat moves the 45s watchdog deadline", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();

    vi.useFakeTimers();
    act(() => {
      first.open();
      vi.advanceTimersByTime(30_000);
      first.frame(JSON.stringify({ role: "heartbeat" }));
      vi.advanceTimersByTime(44_999);
    });
    expect(currentSource()).toBe(first);
    act(() => { vi.advanceTimersByTime(1); });
    expect(currentSource()).not.toBe(first);
  });

  it("three malformed frames schedule a retry without closing the old source", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();

    vi.useFakeTimers();
    act(() => {
      first.open();
      first.frame("{");
      first.frame("{");
      first.frame("{");
    });
    expect(first.readyState).toBe(READY.OPEN);
    act(() => { vi.advanceTimersByTime(999); });
    expect(currentSource()).toBe(first);
    act(() => { vi.advanceTimersByTime(1); });
    expect(currentSource()).not.toBe(first);
    expect(first.readyState).toBe(READY.CLOSED);
  });

  it("unmount cancels a pending CLOSED retry", async () => {
    const mounted = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(mounted.result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();

    vi.useFakeTimers();
    act(() => first.error(READY.CLOSED));
    await act(async () => await Promise.resolve());
    mounted.unmount();
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(currentSource()).toBe(first);
  });
});

describe("AlertsProvider hidden-tab gating", () => {
  it("hidden page closes the stream without polling; visible reopens", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForSource();
    const first = currentSource();
    const queryClient = lastQueryClient();
    await waitFor(() => expect(queryClient.getQueryState(ALERTS_QUERY_KEY)?.fetchStatus).toBe("idle"));
    const reads = gateway.requests.length;

    try {
      vi.useFakeTimers();
      act(() => setVisibility("hidden"));

      // The stream effect closes its EventSource (freeing the connection slot).
      expect(first.readyState).toBe(READY.CLOSED);

      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      // Hidden views do no periodic reads.
      expect(gateway.requests.map((request) => request.path)).toHaveLength(reads);
      expect(queryClient.getQueryState(ALERTS_QUERY_KEY)?.isInvalidated).toBe(false);

      act(() => setVisibility("visible"));

      // Returning visible opens a fresh stream and stops the poll.
      expect(currentSource()).not.toBe(first);
      expect(currentSource().url).toContain("/api/alerts/stream");
      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      expect(gateway.requests.map((request) => request.path)).toHaveLength(reads);
      expect(queryClient.getQueryState(ALERTS_QUERY_KEY)?.isInvalidated).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("AlertsProvider on the shared profile stream", () => {
  it("the leader's alerts source feeds a frame into the cache", async () => {
    installTab("shared");
    snapshot = resp([row({ id: 2 })], 13);
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitFor(() => expect(tab.sources.some((source) => source.url.endsWith("/api/alerts/stream"))).toBe(true));
    const alertsSource = tab.sources.find((source) => source.url.endsWith("/api/alerts/stream"));
    if (!alertsSource) throw new Error("the alerts source was not opened");
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(13));

    act(() => alertsSource.open());
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    const reads = gateway.requests.length;
    snapshot = resp([row({ id: 2 }), row({ id: 3, status: "resolved" })], 12);
    act(() => alertsSource.frame(JSON.stringify(row({ id: 999, status: "resolved" }))));

    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(12));
    expect(gateway.requests.length).toBeGreaterThan(reads);
  });
});
