// useFleetGraph tests — the API fetch with no client-side fallback.
//
// The hook has three observable outcomes:
//   1. while the /api/fleet/graph query is pending -> an empty graph,
//      loading=true, error=false;
//   2. once the query succeeds -> the API graph (normalized), loading=false,
//      error=false;
//   3. if the query errors (404 / gateway down) -> an empty graph, loading=false,
//      error=true (no retry).

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { WireFleetGraph } from "../contracts/types";
import { apiWrapper } from "../transport/tests/api/api-provider";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { useFleetGraph } from "./use-fleet-graph";

const API_GRAPH: WireFleetGraph = {
  nodes: [
    {
      agent_id: 7,
      label: "from-api",
      status: "running",
      liveness_state: "online",
      spawner: "user",
      machine: "test",
      node_score: 0,
      total_tokens: 0,
    },
  ],
  edges: [
    {
      from_agent: 7,
      to_agent: 7,
      event_type: "message",
      weight: 0.5,
      event_count: 2,
      last_seen_at: "2026-06-17T00:00:00Z",
    },
  ],
  telemetry_stale: false,
  snapshot_at: null,
};

let gateway: TestGateway;
let answer: GatewayReply;

function renderGraph(client: QueryClient = freshClient()) {
  const ApiWrapper = apiWrapper(gatewayRig(gateway).api);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(ApiWrapper, {
      children: createElement(QueryClientProvider, { client, children }),
    });
  return renderHook(() => useFleetGraph(), { wrapper });
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(async () => {
  answer = { kind: "json", body: API_GRAPH };
  gateway = await startGateway((request) => request.path === "/api/fleet/graph"
    ? answer : { kind: "text", status: 404, body: "unexpected route" });
});

afterEach(async () => {
  cleanup();
  await gateway.close();
});

describe("useFleetGraph", () => {
  it("while pending: empty graph, loading=true, error=false", async () => {
    answer = { kind: "hang" };
    const { result } = renderGraph();
    await waitFor(() => expect(gateway.requests).toHaveLength(1));

    expect(result.current.graph).toEqual({
      nodes: [],
      edges: [],
      telemetry_stale: false,
      snapshot_at: null,
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("on success: returns the API graph, loading=false, error=false", async () => {
    const { result } = renderGraph();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph).toEqual(API_GRAPH);
    expect(result.current.error).toBe(false);
  });

  it("normalizes the raw backend shape: from_agent/to_agent + send_message->message", async () => {
    const raw: WireFleetGraph = {
      nodes: [],
      edges: [
        { from_agent: 1, to_agent: 2, event_type: "send_message", weight: 0.5, event_count: 3, last_seen_at: "2026-06-30T00:00:00Z" },
        { from_agent: 2, to_agent: 3, event_type: "resurrect", weight: 4, event_count: 2, last_seen_at: "2026-06-30T00:00:00Z" },
      ],
      telemetry_stale: false,
    };

    answer = { kind: "json", body: raw };
    const { result } = renderGraph();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.edges).toEqual([
      { from_agent: 1, to_agent: 2, event_type: "message", weight: 0.5, event_count: 3, last_seen_at: "2026-06-30T00:00:00Z" },
      { from_agent: 2, to_agent: 3, event_type: "resurrect", weight: 4, event_count: 2, last_seen_at: "2026-06-30T00:00:00Z" },
    ]);
  });

  it("projects raw fleet-node lifecycle states to the public status model", async () => {
    const raw: WireFleetGraph = {
      nodes: [
        { ...API_GRAPH.nodes[0], agent_id: 1, status: "running" },
        { ...API_GRAPH.nodes[0], agent_id: 2, status: "idling" },
        { ...API_GRAPH.nodes[0], agent_id: 3, status: "terminated" },
      ],
      edges: [],
      telemetry_stale: false,
    };

    answer = { kind: "json", body: raw };
    const { result } = renderGraph();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.nodes.map((node) => node.status)).toEqual([
      "running",
      "idling",
      "terminated",
    ]);
  });

  it("preserves offline liveness on an idling node", async () => {
    const raw: WireFleetGraph = {
      nodes: [
        {
          ...API_GRAPH.nodes[0],
          status: "idling",
          liveness_state: "offline",
        },
      ],
      edges: [],
      telemetry_stale: false,
      snapshot_at: null,
    };

    answer = { kind: "json", body: raw };
    const { result } = renderGraph();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.nodes[0]).toMatchObject({
      status: "idling",
      liveness_state: "offline",
    });
  });

  it("on error: empty graph, loading=false, error=true (no retry)", async () => {
    answer = { kind: "text", status: 404, body: "no such endpoint" };
    const { result } = renderGraph();

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.graph).toEqual({
      nodes: [],
      edges: [],
      telemetry_stale: false,
      snapshot_at: null,
    });
  });

  it("stale-while-error: a failed poll after data keeps the last good graph (error flagged, not blanked)", async () => {
    const client = freshClient();
    const { result } = renderGraph(client);

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph).toEqual(API_GRAPH);
    expect(result.current.error).toBe(false);

    answer = { kind: "drop" };
    await act(async () => {
      await client.refetchQueries({ queryKey: ["fleet-graph"] });
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.graph).toEqual(API_GRAPH);
  });
});
