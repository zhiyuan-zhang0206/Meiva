// useDisplayLimit — runtime display-window defaults from GET /api/config
// (task #3696). The baked fallback must hold until/unless the config read
// lands: a failed, missing, or non-numeric field never blanks the value.

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  gatewayRig,
  realNetwork, startGateway,
  type GatewayReply,
  type TestGateway,
} from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useDisplayLimit } from "./display-limits";

let activeGateway: TestGateway | null = null;

afterEach(async () => {
  cleanup();
  if (activeGateway !== null) {
    await activeGateway.close();
    activeGateway = null;
  }
});

async function setupConfigGateway(reply: GatewayReply) {
  const gateway = await startGateway(realNetwork.createServer, (req) => {
    if (req.method === "GET" && req.path === "/api/config") {
      return reply;
    }
    return { kind: "json", status: 404, body: { detail: "not found" } };
  });
  activeGateway = gateway;
  const rig = gatewayRig(gateway);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(
      ApiProvider,
      { api: rig.api },
      createElement(QueryClientProvider, { client: queryClient }, children),
    );
  return { gateway, wrapper };
}

describe("useDisplayLimit", () => {
  it("returns the baked fallback while the config read is pending", async () => {
    const { wrapper } = await setupConfigGateway({ kind: "hang" });
    const { result } = renderHook(() => useDisplayLimit("MEIVA_X", 50), { wrapper });
    expect(result.current).toBe(50);
  });

  it("resolves the configured value by env var", async () => {
    const { wrapper } = await setupConfigGateway({
      kind: "json",
      body: {
        fields: [{ env_var: "MEIVA_X", current_value: 137 }],
      },
    });
    const { result } = renderHook(() => useDisplayLimit("MEIVA_X", 50), { wrapper });
    await waitFor(() => expect(result.current).toBe(137));
  });

  it("keeps the fallback when the field is missing", async () => {
    const { gateway, wrapper } = await setupConfigGateway({
      kind: "json",
      body: {
        fields: [{ env_var: "MEIVA_OTHER", current_value: 5 }],
      },
    });
    const { result } = renderHook(() => useDisplayLimit("MEIVA_X", 50), { wrapper });
    await waitFor(() => expect(gateway.requests.length).toBeGreaterThan(0));
    expect(result.current).toBe(50);
  });

  it("keeps the fallback when the config read fails", async () => {
    const { gateway, wrapper } = await setupConfigGateway({
      kind: "json",
      status: 500,
      body: { detail: "boom" },
    });
    const { result } = renderHook(() => useDisplayLimit("MEIVA_X", 50), { wrapper });
    await waitFor(() => expect(gateway.requests.length).toBeGreaterThan(0));
    expect(result.current).toBe(50);
  });

  it("keeps the fallback when the payload is malformed (no fields list)", async () => {
    // The visual-regression e2e intercepts every /api/** request and fulfills
    // unknown endpoints with {} — a malformed payload must not crash the page
    // (user ruling: config unreachable -> baked fallback, never blank). The
    // act flush is load-bearing: the crash would happen when the resolved
    // data re-renders the hook, after the initial render returned.
    const { wrapper } = await setupConfigGateway({
      kind: "json",
      body: {},
    });
    const { result } = renderHook(() => useDisplayLimit("MEIVA_X", 50), { wrapper });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current).toBe(50);
  });
});
