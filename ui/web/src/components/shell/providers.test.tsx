// Providers: QueryClient defaults + root provider composition.

import { QueryClientProvider, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { createContext, useContext, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";


// meiva:note
// The theme service `app/layout.tsx` mounts is next-themes; jsdom has no matchMedia for its
// bootstrap, so the tree is composed with the provider below instead — the same input the
// composition root passes, not a replaced module. It renders its children and answers one scheme.
const fakeTheme: ThemeRuntime = {
  Provider: ({ children, nonce }) => (
    <div data-testid="theme-provider" data-nonce={nonce}>{children}</div>
  ),
  useResolvedTheme: () => "light",
};

// meiva:note
// Authentication stays pending while real root readers own their explicit wire
// boundaries. Settings and themes still read.
// meiva:link ui/web/src/docs/frontend.md

import { ApiError } from "@/lib/transport/api";
import { setSessionInvalidHandler, useAuth } from "@/lib/auth/auth-context";
import { useNonce } from "@/lib/auth/nonce-context";
import { useApi } from "@/lib/transport/transport-context";
import { fakeNavigation } from "@/lib/browser/navigation/testing";
import type { ThemeMode, ThemeRuntime } from "@/lib/layout/theme-runtime";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { resetTransport, transport as fakeTransport } from "@/test-support/fake-api";
import { fakeStreamHub } from "@/test-support/fake-stream";

import { createQueryClient, Providers } from "./providers";

// meiva:note
// The tree's navigation value: the real one comes from the app router, which does not exist in this
// suite, so the composition root is given one — the same way it is given a client and a hub.
const navigation = fakeNavigation();

const clients = new Set<QueryClient>();
const originalLang = document.documentElement.lang;

beforeEach(() => {
  vi.clearAllMocks();
  resetTransport();
});

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
  const provided = useApi();
  const { status } = useAuth();
  useEffect(() => { clients.add(client); }, [client]);
  return (
    <>
      <span data-testid="auth-status">{status}</span>
      <span data-testid="provider-client" data-is-app-client={String(provided === fakeTransport)} />
    </>
  );
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
  it("passes children through and provides the running app's client", () => {
    const { hub, tab } = fakeStreamHub();
    const view = render(
      <Providers client={fakeTransport} hub={hub} navigation={navigation} theme={fakeTheme}>
        <RootProbe />
        <span data-testid="child">child</span>
      </Providers>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    // meiva:note
    // The composition root's job is to hand the app's own client to every reader; the
    // readers of settings / contributions then read through it, so this asserts the
    // identity the provider supplies rather than counting calls on a replaced module.
    expect(screen.getByTestId("provider-client").getAttribute("data-is-app-client")).toBe("true");
    // meiva:note
    // Mounting must not open the stream: the hub's tab constructs a source per channel, so an
    // empty record here is the same statement a spy on the global would make.
    expect(tab.sources).toHaveLength(0);
    view.unmount();
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
  });

  it("passes the request nonce to the theme bootstrap script", () => {
    render(
      <Providers
        client={fakeTransport}
        hub={fakeStreamHub().hub}
        navigation={navigation}
        nonce="request-nonce"
        theme={fakeTheme}
      >
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
      <Providers
        client={fakeTransport}
        hub={fakeStreamHub().hub}
        navigation={navigation}
        nonce="request-nonce"
        theme={fakeTheme}
      >
        <RootProbe />
        <NonceProbe />
      </Providers>,
    );

    expect(screen.getByTestId("nonce-probe").textContent).toBe("request-nonce");
  });

  it("does not reverse-overwrite setTheme on cross-tab storage sync and invalidates user-settings once", async () => {
    let notifyExternalStorage: ((next: ThemeMode) => void) | null = null;
    const setTheme = vi.fn<(next: ThemeMode) => void>();
    const ThemeCtx = createContext<ThemeMode>("system");
    const dynamicTheme: ThemeRuntime = {
      Provider: ({ children }) => {
        const [mode, setMode] = useState<ThemeMode>("system");
        useEffect(() => {
          setTheme.mockImplementation((next: ThemeMode) => setMode(next));
          notifyExternalStorage = (next: ThemeMode) => setMode(next);
          return () => {
            notifyExternalStorage = null;
          };
        }, []);
        return <ThemeCtx.Provider value={mode}>{children}</ThemeCtx.Provider>;
      },
      useResolvedTheme: () => (useContext(ThemeCtx) === "light" ? "light" : "dark"),
      useThemeMode: () => ({ theme: useContext(ThemeCtx), setTheme }),
    };

    let serverTheme: ThemeMode = "system";
    fakeTransport.getSettings.mockImplementation(() =>
      Promise.resolve({
        settings: [{ key: "display.theme", value: serverTheme, updated_at: "2026-11-18T00:00:00Z" }],
      }),
    );

    let capturedClient: QueryClient | null = null;
    function ClientCapture() {
      const client = useQueryClient();
      useEffect(() => {
        clients.add(client);
        capturedClient = client;
      }, [client]);
      return null;
    }

    const hub = fakeStreamHub().hub;
    render(
      <Providers client={fakeTransport} hub={hub} navigation={navigation} theme={dynamicTheme}>
        <ClientCapture />
      </Providers>,
    );

    await waitFor(() =>
      expect(capturedClient?.getQueryState(SETTINGS_QUERY_KEY)?.status).toBe("success"),
    );
    expect(fakeTransport.getSettings).toHaveBeenCalledTimes(1);
    expect(setTheme).not.toHaveBeenCalled();

    // meiva:note
    // Simulate another tab updating DB `display.theme` to `"light"` and broadcasting
    // `localStorage.theme = "light"` via `storage` while this tab's query cache is still `"system"`.
    // Repeated external updates before refetch settles must neither reverse-call `setTheme("system")` nor storm `getSettings`.
    serverTheme = "light";
    act(() => {
      notifyExternalStorage?.("light");
    });
    act(() => {
      notifyExternalStorage?.("light");
    });

    expect(setTheme).not.toHaveBeenCalled();
    await waitFor(() => expect(fakeTransport.getSettings).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(
        capturedClient?.getQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY)?.["display.theme"],
      ).toBe("light"),
    );
    expect(setTheme).not.toHaveBeenCalled();

    // meiva:note
    // An explicit local `display.theme` change in this tab still calls `setTheme` immediately.
    act(() => {
      capturedClient?.setQueryData(SETTINGS_QUERY_KEY, { "display.theme": "dark" });
    });
    await waitFor(() => expect(setTheme).toHaveBeenCalledTimes(1));
    expect(setTheme).toHaveBeenLastCalledWith("dark");
  });
});
