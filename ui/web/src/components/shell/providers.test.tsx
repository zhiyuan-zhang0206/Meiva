// Providers: QueryClient defaults + root provider composition.

import { QueryClientProvider, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/transport/api";

// EventStreamProvider opens a real EventSource (happy-dom ships one that hits
// the network); stub it to a pass-through so the smoke test stays offline. The
// connection behavior itself is covered in useEventStream.test.ts. With the
// module mocked, the root AgentsCacheSync subscriber's useEventStream is a noop.
vi.mock("@/lib/transport/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: vi.fn(),
}));

vi.mock("next-themes", () => ({
  ThemeProvider: ({ children, nonce }: { children: ReactNode; nonce?: string }) => (
    <div data-testid="theme-provider" data-nonce={nonce}>{children}</div>
  ),
  useTheme: () => ({ resolvedTheme: "light" }),
}));

// meiva:note
// Authentication stays pending while real root readers own their explicit wire
// boundaries. Alert I/O is asserted blocked; settings and themes still read.
// meiva:link ui/web/src/docs/frontend.meiva.okf.md
vi.mock("@/lib/transport/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getClusterStatus: vi.fn(() => new Promise(() => undefined)),
      checkAuth: vi.fn(() => new Promise(() => undefined)),
      getSettings: vi.fn<typeof actual.api.getSettings>(() => new Promise(() => undefined)),
      getUiContributions: vi.fn<typeof actual.api.getUiContributions>(() => new Promise(() => undefined)),
      getAlerts: vi.fn<typeof actual.api.getAlerts>(() => new Promise(() => undefined)),
    },
  };
});

import { ApiError, api } from "@/lib/transport/api";
import { ProfileSseTransport } from "@/lib/transport/sse-share";
import { setSessionInvalidHandler, useAuth } from "@/lib/auth/auth-context";
import { useNonce } from "@/lib/auth/nonce-context";

import { createQueryClient, Providers } from "./providers";

const clients = new Set<QueryClient>();
const originalLang = document.documentElement.lang;

beforeEach(() => { vi.clearAllMocks(); });

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  document.documentElement.lang = originalLang;
  vi.restoreAllMocks();
  setSessionInvalidHandler(null);
  vi.useRealTimers();
});

function testQueryClient() {
  const client = createQueryClient();
  clients.add(client);
  return client;
}

function RootProbe() {
  const client = useQueryClient();
  const { status } = useAuth();
  useEffect(() => { clients.add(client); }, [client]);
  return <span data-testid="auth-status">{status}</span>;
}

function QueryProbe({ queryFn }: { queryFn: () => Promise<unknown> }) {
  useQuery({ queryKey: ["query-client-probe"], queryFn });
  return null;
}

describe("QueryClient defaults", () => {
  it("routes a 401 to auth without retrying the query", async () => {
    const sessionInvalidHandler = vi.fn();
    const queryFn = vi
      .fn()
      .mockRejectedValue(new ApiError(401, "authentication required"));
    setSessionInvalidHandler(sessionInvalidHandler);

    render(
      <QueryClientProvider client={testQueryClient()}>
        <QueryProbe queryFn={queryFn} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(sessionInvalidHandler).toHaveBeenCalledTimes(1));
    expect(queryFn).toHaveBeenCalledTimes(1);
  });

  it("caps transient-failure retries at two (three attempts, task #3895)", async () => {
    vi.useFakeTimers();
    const queryFn = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));

    render(
      <QueryClientProvider client={testQueryClient()}>
        <QueryProbe queryFn={queryFn} />
      </QueryClientProvider>,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(queryFn).toHaveBeenCalledTimes(3);
  });

  it("retries only transient statuses and caps the retry delay (task #3895)", () => {
    const client = testQueryClient();
    const retry = client.getDefaultOptions().queries?.retry as (
      failureCount: number,
      error: unknown,
    ) => boolean;
    expect(retry(0, new ApiError(401, "auth"))).toBe(false);
    expect(retry(0, new ApiError(404, "not found"))).toBe(false);
    expect(retry(0, new ApiError(422, "unprocessable"))).toBe(false);
    expect(retry(0, new ApiError(408, "timeout"))).toBe(true);
    expect(retry(0, new ApiError(429, "rate limited"))).toBe(true);
    expect(retry(0, new ApiError(503, "unavailable"))).toBe(true);
    expect(retry(1, new TypeError("Failed to fetch"))).toBe(true);
    expect(retry(2, new TypeError("Failed to fetch"))).toBe(false);
    const retryDelay = client.getDefaultOptions().queries?.retryDelay as (n: number) => number;
    expect(retryDelay(0)).toBe(1_000);
    expect(retryDelay(1)).toBe(2_000);
    expect(retryDelay(6)).toBe(8_000);
  });
});

describe("Providers", () => {
  it("passes children through", async () => {
    const sources = vi.spyOn(globalThis, "EventSource");
    const subscriptions = vi.spyOn(ProfileSseTransport.prototype, "subscribe");
    const view = render(
      <Providers>
        <RootProbe />
        <span data-testid="child">child</span>
      </Providers>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    await waitFor(() => expect(api.checkAuth).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("auth-status").textContent).toBe("loading");
    expect(api.getSettings).toHaveBeenCalledTimes(1);
    expect(api.getUiContributions).toHaveBeenCalledTimes(1);
    expect(api.getAlerts).not.toHaveBeenCalled();
    expect(api.getClusterStatus).not.toHaveBeenCalled();
    expect(sources).not.toHaveBeenCalled();
    expect(subscriptions).not.toHaveBeenCalled();
    view.unmount();
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
  });

  it("passes the request nonce to the theme bootstrap script", () => {
    render(
      <Providers nonce="request-nonce">
        <RootProbe />
        <span>child</span>
      </Providers>,
    );

    expect(screen.getByTestId("theme-provider").dataset.nonce).toBe("request-nonce");
  });

  it("exposes the request nonce to the subtree", () => {
    function NonceProbe() {
      return <span data-testid="nonce-probe">{useNonce() ?? "(none)"}</span>;
    }

    render(
      <Providers nonce="request-nonce">
        <RootProbe />
        <NonceProbe />
      </Providers>,
    );

    expect(screen.getByTestId("nonce-probe").textContent).toBe("request-nonce");
  });
});
