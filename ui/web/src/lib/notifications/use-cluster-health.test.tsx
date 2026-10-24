// Gate-only maintenance ownership + cluster reconnect edge.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useStore } from "../state/store";
import { AGENTS_QUERY_KEY } from "../agents/use-agents";
import type { ClusterStatus } from "../contracts/types";
import { apiWrapper, fakeApi } from "../transport/tests/api/fake-api";
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

// meiva:note
// The host's two status endpoints as one in-memory service. `paused` is the host
// state the next cluster-status read reports; the system-status endpoint is the
// heavier one the hook must not read, so it counts any read and fails it.
class Host {
  paused = false;
  clusterReads = 0;
  systemReads = 0;
  readonly api = fakeApi({
    getClusterStatus: () => {
      this.clusterReads += 1;
      return Promise.resolve(clusterStatus(this.paused));
    },
    getSystemStatus: () => {
      this.systemReads += 1;
      return Promise.reject(new Error("the system status endpoint must not be read"));
    },
  });
}

let host: Host;

function renderHealth(client: QueryClient) {
  const ApiWrapper = apiWrapper(host.api);
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

beforeEach(() => {
  host = new Host();
  act(() => useStore.setState({ reconnectNonce: 0 }));
});

afterEach(cleanup);

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
    host.paused = true;
    renderHealth(client);

    await waitFor(() =>
      expect(client.getQueryData(CLUSTER_STATUS_QUERY_KEY)).toMatchObject({ paused: true }),
    );
    const nonce = useStore.getState().reconnectNonce;
    expect(agentReads).toBe(1);
    host.paused = false;
    await act(async () => {
      await client.refetchQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    });

    await waitFor(() => expect(useStore.getState().reconnectNonce).toBe(nonce + 1));
    await waitFor(() => expect(agentReads).toBe(2));
  });


  it("never touches the heavier system status endpoint", async () => {
    renderHealth(freshClient());

    await waitFor(() => expect(host.clusterReads).toBeGreaterThan(0));
    expect(host.systemReads).toBe(0);
  });
});
