// Gate-only maintenance ownership + cluster reconnect edge.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useStore } from "../state/store";
import { AGENTS_QUERY_KEY } from "../agents/use-agents";
import type { ClusterStatus } from "../contracts/types";
import { apiWrapper } from "../transport/tests/api/api-provider";
import { startGateway, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import {
  CLUSTER_STATUS_QUERY_KEY,
  useClusterHealth,
} from "./use-cluster-health";

function clusterStatus(paused: boolean): ClusterStatus {
  return {
    machine_name: "test-host",
    serve_gateway: true,
    serve_agent_runner: true,
    serve_observability_station: false,
    paused,
    shell_count: 0,
    agent_count: 0,
    session_count: 0,
    agent_groups: [],
  };
}

let paused: boolean;
let gateway: TestGateway;

function renderHealth(client: QueryClient) {
  const ApiWrapper = apiWrapper(gatewayRig(gateway).api);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ApiWrapper>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ApiWrapper>
  );
  return renderHook(() => useClusterHealth(), { wrapper });
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(async () => {
  paused = false;
  gateway = await startGateway((request) => request.path === "/api/cluster/status"
    ? { kind: "json", body: clusterStatus(paused) }
    : { kind: "text", status: 500, body: "the heavier status endpoint must not be read" });
  act(() => useStore.setState({ reconnectNonce: 0 }));
});

afterEach(async () => {
  cleanup();
  await gateway.close();
});

describe("useClusterHealth", () => {
  it("paused true to false reconnects SSE and refetches agents", async () => {
    const client = freshClient();
    let agentReads = 0;
    await client.query({
      queryKey: AGENTS_QUERY_KEY,
      queryFn: () => {
        agentReads += 1;
        return Promise.resolve([]);
      },
    });
    paused = true;
    renderHealth(client);

    await waitFor(() =>
      expect(client.getQueryData(CLUSTER_STATUS_QUERY_KEY)).toMatchObject({ paused: true }),
    );
    const nonce = useStore.getState().reconnectNonce;
    expect(agentReads).toBe(1);
    paused = false;
    await act(async () => {
      await client.refetchQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    });

    await waitFor(() => expect(useStore.getState().reconnectNonce).toBe(nonce + 1));
    await waitFor(() => expect(agentReads).toBe(2));
  });


  it("never touches the heavier system status endpoint", async () => {
    renderHealth(freshClient());

    await waitFor(() => expect(gateway.requests).toHaveLength(1));
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/cluster/status"]);
  });
});
