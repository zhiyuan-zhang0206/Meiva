// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { USER_SETTING_DEFAULTS } from "../contracts/types";
import { SETTINGS_QUERY_KEY } from "../state/use-user-settings";
import {
  cacheOnlyApi,
  gatewayRig,
  realNetwork, startGateway,
  type TestGateway,
} from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useContentToggle, useContentToggleReset } from "./content-toggle-store";

let activeGateway: TestGateway | null = null;

afterEach(async () => {
  cleanup();
  if (activeGateway !== null) {
    await activeGateway.close();
    activeGateway = null;
  }
});

function createSeededWrapper(initial: Record<string, unknown> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(SETTINGS_QUERY_KEY, {
    ...USER_SETTING_DEFAULTS,
    ...initial,
  });
  const api = cacheOnlyApi();
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      ApiProvider,
      { api },
      createElement(QueryClientProvider, { client }, children),
    );
  };
}

describe("useContentToggle", () => {
  it('no stored value → detailsMode defaults to "none"', () => {
    const { result } = renderHook(() => useContentToggle(), {
      wrapper: createSeededWrapper(),
    });
    expect(result.current.detailsMode).toBe("none");
  });

  it("surfaces isLoading from the settings query (consumers gate expansion on it)", () => {
    const { result } = renderHook(() => useContentToggle(), {
      wrapper: createSeededWrapper(),
    });
    expect(result.current.isLoading).toBe(false);
  });

  it('detailsMode reads from display.expand_runs_mode', () => {
    const { result } = renderHook(() => useContentToggle(), {
      wrapper: createSeededWrapper({ "display.expand_runs_mode": "none" }),
    });
    expect(result.current.detailsMode).toBe("none");
  });

  it('unrecognized stored value → falls back to "none"', () => {
    const { result } = renderHook(() => useContentToggle(), {
      wrapper: createSeededWrapper({ "display.expand_runs_mode": "expanded" }),
    });
    expect(result.current.detailsMode).toBe("none");
  });

  it('setDetailsMode writes display.expand_runs_mode="last"', async () => {
    const gateway = await startGateway(realNetwork.createServer, (req) => {
      if (req.method === "GET" && req.path === "/api/settings") {
        return { kind: "json", body: { settings: [] } };
      }
      if (req.method === "PUT" && req.path === "/api/settings/display.expand_runs_mode") {
        const parsed = JSON.parse(req.body) as { value: unknown };
        return {
          kind: "json",
          body: {
            key: "display.expand_runs_mode",
            value: parsed.value,
            updated_at: "2026-01-01T00:00:00Z",
          },
        };
      }
      return { kind: "json", status: 404, body: { detail: "not found" } };
    });
    activeGateway = gateway;
    const rig = gatewayRig(gateway);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        ApiProvider,
        { api: rig.api },
        createElement(QueryClientProvider, { client }, children),
      );

    const { result } = renderHook(() => useContentToggle(), { wrapper });
    act(() => result.current.setDetailsMode("last"));

    await waitFor(() => {
      const puts = gateway.requests.filter(
        (r) => r.method === "PUT" && r.path === "/api/settings/display.expand_runs_mode",
      );
      expect(puts).toHaveLength(1);
      expect(JSON.parse(puts[0].body)).toEqual({ value: "last" });
    });
  });
});

describe("useContentToggleReset", () => {
  it("a re-pick leaves a pending reset for the next mounted timeline", () => {
    const { result } = renderHook(() => useContentToggleReset());
    act(() => result.current.bumpReset());
    expect(result.current.resetPending).toBe(true);
  });

  it("consuming the pending reset clears the flag", () => {
    const { result } = renderHook(() => useContentToggleReset());
    act(() => result.current.bumpReset());
    act(() => result.current.consumeReset());
    expect(result.current.resetPending).toBe(false);
  });
});
