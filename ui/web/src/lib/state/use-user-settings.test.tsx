// useUserSettings hook tests — query loading, optimistic update, error rollback.

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { useDebouncedSetting, useUserSettings } from "./use-user-settings";
import {
  gatewayRig,
  realNetwork, startGateway,
  type GatewayReply,
  type TestGateway,
} from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";

let activeGateway: TestGateway | null = null;

afterEach(async () => {
  cleanup();
  if (activeGateway !== null) {
    await activeGateway.close();
    activeGateway = null;
  }
});

async function setupSettingsGateway(
  handler: {
    onGet?: () => GatewayReply | Promise<GatewayReply>;
    onPut?: (key: string, value: unknown) => GatewayReply | Promise<GatewayReply>;
  } = {},
) {
  const gateway = await startGateway(realNetwork.createServer, async (request) => {
    if (request.method === "GET" && request.path === "/api/settings") {
      if (handler.onGet) return handler.onGet();
      return { kind: "json", body: { settings: [] } };
    }
    if (request.method === "PUT" && request.path.startsWith("/api/settings/")) {
      const key = decodeURIComponent(request.path.slice("/api/settings/".length));
      const parsed = JSON.parse(request.body) as { value: unknown };
      if (handler.onPut) return handler.onPut(key, parsed.value);
      return {
        kind: "json",
        body: {
          key,
          value: parsed.value,
          updated_at: "2026-01-01T00:00:00Z",
        },
      };
    }
    return { kind: "json", status: 404, body: { detail: "not found" } };
  });
  activeGateway = gateway;
  const rig = gatewayRig(gateway);
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ApiProvider api={rig.api}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </ApiProvider>
  );
  return { gateway, rig, qc, wrapper };
}

function putRequests(gateway: TestGateway): { key: string; value: unknown }[] {
  return gateway.requests
    .filter((req) => req.method === "PUT" && req.path.startsWith("/api/settings/"))
    .map((req) => ({
      key: decodeURIComponent(req.path.slice("/api/settings/".length)),
      value: (JSON.parse(req.body) as { value: unknown }).value,
    }));
}

describe("useUserSettings", () => {
  it("returns defaults when server returns empty", async () => {
    const { wrapper } = await setupSettingsGateway();

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.settings["display.show_timestamp_weekday"]).toBe(true);
    expect(result.current.settings["display.time_mode"]).toBe("last_active");
    expect(result.current.settings["display.clock_format"]).toBe("24h");
  });

  it("merges server values over defaults", async () => {
    const { wrapper } = await setupSettingsGateway({
      onGet: () => ({
        kind: "json",
        body: {
          settings: [
            {
              key: "display.show_timestamp_weekday",
              value: false,
              updated_at: "2026-01-01T00:00:00Z",
            },
          ],
        },
      }),
    });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.settings["display.show_timestamp_weekday"]).toBe(false);
    expect(result.current.settings["display.time_mode"]).toBe("last_active");
  });

  it("setSetting rolls back on server error", async () => {
    const { wrapper } = await setupSettingsGateway({
      onPut: () => ({
        kind: "json",
        status: 500,
        body: { detail: "Server error" },
      }),
    });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Initial value from defaults
    expect(result.current.settings["display.show_timestamp_weekday"]).toBe(true);

    // Attempt to update — should fail and roll back
    result.current.setSetting("display.show_timestamp_weekday", false);

    // After error, value should still be the default (rolled back)
    await waitFor(() => {
      expect(result.current.settings["display.show_timestamp_weekday"]).toBe(true);
    });
  });

  // meiva:note
  // The read never answers, so the rollback can only be seen once the PUT's rejection lands.
  // Both halves are settled by hand and asserted on the call itself: no `waitFor`, so a
  // loaded machine cannot turn this into a failure and a real regression cannot hide behind
  // a longer timeout.
  // meiva:note
  // The GET never answers and the PUT is released by hand. Both halves are events the test
  // waits for (the request reaching the gateway, the query cache taking the rolled-back
  // value), not clocks: a loaded machine cannot turn this into a failure, and a real
  // regression cannot hide behind a longer timeout.
  it("rolls back to defaults when the PUT fails before the initial GET resolves", async () => {
    let releasePut: (reply: GatewayReply) => void = () => undefined;
    let putArrived: () => void = () => undefined;
    const arrived = new Promise<void>((resolve) => {
      putArrived = resolve;
    });
    const { qc, wrapper } = await setupSettingsGateway({
      onGet: () => ({ kind: "hang" }),
      onPut: () => {
        putArrived();
        return new Promise<GatewayReply>((resolve) => {
          releasePut = resolve;
        });
      },
    });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    // Default (quiet) value before the toggle.
    expect(result.current.settings["display.show_agent_status"]).toBe(false);

    await act(async () => {
      result.current.setSetting("display.show_agent_status", true);
      await arrived;
      await Promise.resolve();
    });

    // meiva:note
    // Optimistic flip lands even with the GET unresolved.
    expect(result.current.settings["display.show_agent_status"]).toBe(true);

    // meiva:note
    // The failed PUT rolls it back to the default, not a stuck true. The assertion reads the
    // cache the hook derives `settings` from, and waits for the cache to take that value.
    const rolledBack = new Promise<void>((resolve) => {
      const unsubscribe = qc.getQueryCache().subscribe((event) => {
        if (event.type !== "updated") return;
        const data = event.query.state.data as Record<string, unknown> | undefined;
        if (data?.["display.show_agent_status"] === false) {
          unsubscribe();
          resolve();
        }
      });
    });
    await act(async () => {
      releasePut({ kind: "json", status: 500, body: { detail: "Server error" } });
      await rolledBack;
    });
    expect(qc.getQueryData<Record<string, unknown>>(["user-settings"])?.["display.show_agent_status"]).toBe(false);
  });

  it("caches settings and does not refetch on every call", async () => {
    const { gateway, wrapper } = await setupSettingsGateway();

    // First render
    const { result, rerender } = renderHook(() => useUserSettings(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const getCount = () =>
      gateway.requests.filter((r) => r.method === "GET" && r.path === "/api/settings").length;
    expect(getCount()).toBe(1);

    // Rerender — should not refetch (staleTime = 60s)
    rerender();
    expect(getCount()).toBe(1);
  });

  it("setSetting optimistically updates and calls PUT", async () => {
    const { gateway, wrapper } = await setupSettingsGateway();

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    result.current.setSetting("display.show_timestamp_weekday", false);

    await waitFor(() => {
      expect(putRequests(gateway)).toEqual([
        { key: "display.show_timestamp_weekday", value: false },
      ]);
    });
  });
});

// useDebouncedSetting — the write path for dragged DB settings (timeline
// width, force-layout sliders): local value live immediately, DB PUT
// coalesced, and a pending write flushed on unmount.
describe("useDebouncedSetting", () => {
  it("updates the local value immediately but debounces the PUT", async () => {
    const { gateway, wrapper } = await setupSettingsGateway();
    const { result } = renderHook(
      () => useDebouncedSetting("display.timeline_width_px", 800, 50),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(800));

    act(() => result.current[1](864));
    // Local value is live immediately (for a smooth drag)…
    expect(result.current[0]).toBe(864);
    // …but the network write has not fired yet.
    expect(putRequests(gateway)).toEqual([]);

    // After the debounce window, exactly one PUT lands with the final value.
    await waitFor(() =>
      expect(putRequests(gateway)).toEqual([
        { key: "display.timeline_width_px", value: 864 },
      ]),
    );
  });

  it("coalesces a rapid burst of changes into one trailing PUT", async () => {
    const { gateway, wrapper } = await setupSettingsGateway();
    const { result } = renderHook(
      () => useDebouncedSetting("display.timeline_width_px", 800, 50),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(800));

    act(() => result.current[1](832));
    act(() => result.current[1](864));
    act(() => result.current[1](896));

    await waitFor(() =>
      expect(putRequests(gateway)).toEqual([
        { key: "display.timeline_width_px", value: 896 },
      ]),
    );
  });

  it("flushes a pending write on unmount", async () => {
    const { gateway, wrapper } = await setupSettingsGateway();
    const { result, unmount } = renderHook(
      // Long delay so the timer never fires on its own — only the unmount flush can.
      () => useDebouncedSetting("display.timeline_width_px", 800, 10_000),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(800));

    act(() => result.current[1](960));
    expect(putRequests(gateway)).toEqual([]);

    unmount();
    await waitFor(() =>
      expect(putRequests(gateway)).toEqual([
        { key: "display.timeline_width_px", value: 960 },
      ]),
    );
  });
});
