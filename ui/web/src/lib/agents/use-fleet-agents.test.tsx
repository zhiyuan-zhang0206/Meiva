// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRoster, AgentRow } from "../contracts/types";
import type { Api } from "../transport/api";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useFleetAgents } from "./use-fleet-agents";

const AGENTS = [{ agent_id: 7, harness: "meiva", label: "fleet worker" }] as AgentRow[];

const getAgentRoster = vi.fn<Api["getAgentRoster"]>();

function wrapper({ children }: { children: React.ReactNode }) {
  const testApi: Api = {
    ...cacheOnlyApi(),
    getAgentRoster,
  };
  return React.createElement(
    ApiProvider,
    { api: testApi },
    React.createElement(
      QueryClientProvider,
      { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
      children,
    ),
  );
}

afterEach(() => {
  getAgentRoster.mockReset();
});

describe("useFleetAgents", () => {
  it("returns one stable empty list while the shared roster is loading", () => {
    getAgentRoster.mockReturnValue(new Promise<AgentRoster>(() => undefined));
    const { result, rerender } = renderHook(() => useFleetAgents(), { wrapper });

    const empty = result.current;
    rerender();
    expect(result.current).toBe(empty);
    expect(result.current).toEqual([]);
  });

  it("reads the live shared roster through its cache fetcher", async () => {
    getAgentRoster.mockResolvedValue({ agents: AGENTS, ancestors: [] });
    const { result } = renderHook(() => useFleetAgents(), { wrapper });

    await waitFor(() => expect(result.current).toBe(AGENTS));
    expect(getAgentRoster).toHaveBeenCalled();
  });
});
