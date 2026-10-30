import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENTS_QUERY_KEY } from "@/lib/agents/use-agents";
import type {
  AgentRow,
  WireFleetGraph,
  WireFleetGraphEdge,
  WireFleetGraphNode,
} from "@/lib/contracts/types";

import { GraphView } from "../graph-view";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";

const push = vi.fn();
// meiva:note
// The one replacement left in this file: the view navigates through Next's router, which needs a
// mounted app router. It leaves with the third-party-patch family.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

const { getAgentRoster } = vi.hoisted(() => ({ getAgentRoster: vi.fn() }));

function node(agent_id: number, over: Partial<WireFleetGraphNode> = {}): WireFleetGraphNode {
  return {
    agent_id,
    label: null,
    status: "running",
    liveness_state: "online",
    spawner: "user",
    machine: "test",
    node_score: 0,
    total_tokens: 0,
    ...over,
  };
}

function edge(
  from_agent: number,
  to_agent: number,
  event_type: string,
  over: Partial<WireFleetGraphEdge> = {},
): WireFleetGraphEdge {
  return {
    from_agent,
    to_agent,
    event_type,
    weight: 1,
    event_count: 1,
    last_seen_at: "2026-06-17T00:00:00Z",
    ...over,
  };
}

function rosterRow(agent_id: number, over: Partial<AgentRow> = {}): AgentRow {
  return {
    agent_id,
    harness: "meiva",
    spawner: "user",
    fork_source_agent_id: null,
    status: "terminated",
    spawned_at: "2026-06-17T00:00:00Z",
    last_active_at: "2026-06-17T00:00:00Z",
    last_inbound_at: "2026-06-17T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: false,
    liveness_state: "online",
    awaiting_response_count: 0, highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    ...over,
  };
}

type GraphFetch = "ok" | "pending" | "reject";

// meiva:note
// A request that never settles is how a test holds the cold loading state: the fetch the query is
// waiting on stays unresolved, so the view keeps its first-load placeholder.
function neverSettles(): Promise<WireFleetGraph> {
  return new Promise<WireFleetGraph>(() => undefined);
}

const EMPTY_GRAPH: WireFleetGraph = {
  nodes: [],
  edges: [],
  telemetry_stale: false,
  snapshot_at: null,
};

// meiva:note
// The test's door to the graph: the endpoint answers what the test states (or stays pending, or
// fails), and the roster cache holds the agents the view resolves lineage against. Both enter the
// same providers the app mounts, so no hook module is replaced; the settings cache is seeded empty
// so the force sliders read defaults instead of fetching.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function renderGraph(
  ui: ReactElement,
  {
    graph = EMPTY_GRAPH,
    roster,
    fetch = "ok",
  }: {
    graph?: WireFleetGraph;
    roster?: { live?: AgentRow[]; terminated?: AgentRow[] };
    fetch?: GraphFetch;
  } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(AGENTS_QUERY_KEY, {
    agents: roster?.live ?? [],
    ancestors: roster?.terminated ?? [],
  });
  if (fetch === "pending") {
    transport.getFleetGraph.mockImplementation(neverSettles);
  } else if (fetch === "reject") {
    transport.getFleetGraph.mockRejectedValue(new Error("gateway unavailable"));
  } else {
    transport.getFleetGraph.mockResolvedValue(graph);
  }
  return renderWithProviders(ui, { queryClient, settings: {}, wallNow: () => Date.now() });
}

function queryNodeLabel(id: number): HTMLElement | null {
  return (
    screen
      .queryAllByText(`#${id}`)
      .find((element) => element.tagName.toLowerCase() === "tspan") ?? null
  );
}

function getNodeLabel(id: number): HTMLElement {
  const label = queryNodeLabel(id);
  if (!label) throw new Error(`node label #${id} not rendered`);
  return label;
}

beforeEach(() => {
  resetTransport();
  push.mockReset();
  getAgentRoster.mockReset();
  transport.getAgentRoster.mockImplementation(getAgentRoster);
});

afterEach(cleanup);

describe("GraphView lineage, hover, and empty states", () => {
  it("shows rendered-set counts and the empty state when every node is terminated", async () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      graph: {
        nodes: [node(1, { status: "terminated" }), node(2, { status: "terminated" })],
        edges: [edge(1, 2, "spawn")],
        telemetry_stale: false,
        snapshot_at: null,
      },
    });

    // meiva:note
    // Wait for the resolved paint, not the stats line: the stats count zero nodes while the first
    // fetch is still in flight, so it alone would pass against the loading placeholder.
    await waitFor(() => expect(screen.getByText("No agents to graph.")).toBeTruthy());
    expect(screen.getByText("0 nodes · 0 edges")).toBeTruthy();
  });

  it.each(["spawn", "fork"] as const)("re-parents %s descendants to their nearest live ancestor with solid edges when intermediate parents terminate", async (kind) => {
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
      {
        graph: {
          nodes: [
            node(1, { label: "grandparent", spawner: "user" }),
            node(3, { label: "child", status: "idling", spawner: "agent:2" }),
          ],
          edges: [],
          telemetry_stale: false,
          snapshot_at: null,
        },
        roster: {
          live: [rosterRow(3, {
            status: "idling",
            spawner: kind === "fork" ? "user" : "agent:2",
            fork_source_agent_id: kind === "fork" ? 2 : null,
          })],
          terminated: [rosterRow(2, { spawner: "agent:1" })],
        },
      },
    );

    const label1 = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    expect(label1).toBeTruthy();
    expect(queryNodeLabel(2)).toBeNull();
    expect(queryNodeLabel(3)).not.toBeNull();
    expect(screen.getByText("2 nodes · 1 edges")).toBeTruthy();

    const svg = container.querySelector('svg[aria-label="Fleet relationship graph"]')!;
    expect(svg.querySelectorAll("line").length).toBe(1);
    expect(svg.querySelector("line")!.getAttribute("stroke-dasharray")).toBeNull();
  });

  it.each([false, true])("renders roster fork lineage as solid with telemetry=%s", async (withTelemetry) => {
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
      {
        graph: {
          nodes: [node(1), node(2)],
          edges: withTelemetry ? [edge(1, 2, "spawn", { weight: 4 })] : [],
          telemetry_stale: false,
          snapshot_at: null,
        },
        roster: {
          live: [rosterRow(2, {
            status: "running",
            spawner: "agent:99",
            fork_source_agent_id: 1,
          })],
        },
      },
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const lines = container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line');
    expect(lines.length).toBe(1);
    expect(lines[0].getAttribute("stroke-dasharray")).toBeNull();
    expect(Number(lines[0].getAttribute("stroke-opacity"))).toBeCloseTo(0.72);
    expect(Number(lines[0].getAttribute("stroke-width"))).toBeCloseTo(
      1.2 + Math.sqrt(withTelemetry ? 4 : 2) * 1.2,
    );
  });

  it("fetches one coherent tree on a direct fleet load", async () => {
    getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
    transport.getFleetGraph.mockResolvedValue(EMPTY_GRAPH);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      queryClient: qc,
      settings: {},
      wallNow: () => Date.now(),
    });
    await waitFor(() => expect(getAgentRoster).toHaveBeenCalledTimes(1));
    expect(getAgentRoster).toHaveBeenCalledWith(expect.any(AbortSignal));
  });

  it("cold paint waits for the coherent tree including required ancestors", async () => {
    let finish!: (value: { agents: AgentRow[]; ancestors: AgentRow[] }) => void;
    getAgentRoster.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    transport.getFleetGraph.mockResolvedValue({
      nodes: [node(1), node(3, { spawner: "agent:2" })],
      edges: [],
      telemetry_stale: false,
      snapshot_at: null,
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      queryClient: qc,
      settings: {},
      wallNow: () => Date.now(),
    });
    await waitFor(() => expect(getAgentRoster).toHaveBeenCalledTimes(1));
    expect(queryNodeLabel(3)).toBeNull();
    await act(async () => { finish({ agents: [rosterRow(1, { status: "running" }), rosterRow(3, { status: "running", spawner: "agent:2" })], ancestors: [rosterRow(2, { spawner: "agent:1" })] }); await Promise.resolve(); });
    await waitFor(() => expect(queryNodeLabel(3)).not.toBeNull());
    expect(screen.getByText("2 nodes · 1 edges")).toBeTruthy();
  });

  it("renders no degradation marker for a fresh graph", async () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      graph: {
        nodes: [node(1), node(2)],
        edges: [edge(1, 2, "spawn")],
        telemetry_stale: false,
        snapshot_at: null,
      },
    });

    await waitFor(() => expect(queryNodeLabel(1)).not.toBeNull());
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("renders no telemetry-degraded copy (task #3893)", async () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      graph: {
        nodes: [node(1), node(2)],
        edges: [edge(1, 2, "spawn")],
        telemetry_stale: true,
        snapshot_at: null,
      },
    });

    await waitFor(() => expect(queryNodeLabel(1)).not.toBeNull());
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/telemetry/i)).toBeNull();
  });

  it("merges multiple lineage kinds per pair into one edge (no duplicate React keys)", async () => {
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />,
      {
        graph: {
          nodes: [node(1), node(2), node(3)],
          edges: [
            edge(1, 2, "spawn"),
            edge(1, 2, "resurrect"),
            edge(1, 3, "spawn"),
            edge(1, 3, "fork"),
            edge(1, 3, "message"),
          ],
          telemetry_stale: false,
          snapshot_at: null,
        },
      },
    );

    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    const lines = svg.querySelectorAll("line");
    expect(lines.length).toBe(3);
    for (const line of lines) {
      expect(line.getAttribute("stroke-dasharray")).toBeNull();
    }
  });

  it("shows the hover card instantly on mouseenter and hides it on mouseleave", async () => {
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, {
      graph: {
        nodes: [node(1, { label: "alpha", node_score: 12_345_678 }), node(2)],
        edges: [edge(1, 2, "spawn")],
        telemetry_stale: false,
        snapshot_at: null,
      },
    });
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    const svg = container.querySelector('svg[aria-label="Fleet relationship graph"]')!;
    const canvas = svg.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 600, height: 400 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 100, y: 100, width: 36, height: 36 }),
    );
    const restoreCard = mockCardSize(200, 100);
    try {
      fireEvent.mouseEnter(group);
      expect(screen.queryByRole("tooltip")).not.toBeNull();
      const card = screen.getByRole("tooltip");
      expect(card.textContent).toContain("alpha");
      expect(card.textContent).toContain("Agent #1");
      expect(card.textContent).toContain("Running");
      expect(card.textContent).toContain("Activity score: 12.35M");

      fireEvent.mouseLeave(group);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restoreCard();
    }
  });

  it("empty graph (not loading, no error) shows the empty placeholder", async () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("No agents to graph.")).toBeTruthy());
    expect(screen.getByText("0 nodes · 0 edges")).toBeTruthy();
  });

  it("empty graph while loading shows the loading placeholder", () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, { fetch: "pending" });

    expect(screen.getByText("Loading...")).toBeTruthy();
  });

  it("error (endpoint absent / gateway down) shows the unavailable placeholder", async () => {
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} />, { fetch: "reject" });

    await waitFor(() => expect(screen.getByText("Graph unavailable.")).toBeTruthy());
  });
});

function mockCardSize(width: number, height: number): () => void {
  const proto = HTMLElement.prototype;
  const widthDesc = Object.getOwnPropertyDescriptor({ offsetWidth: 0 }, "offsetWidth");
  const heightDesc = Object.getOwnPropertyDescriptor({ offsetHeight: 0 }, "offsetHeight");
  const isCard = function (this: HTMLElement) {
    return this.getAttribute("role") === "tooltip";
  };
  Object.defineProperty(proto, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return width;
      const real: unknown = widthDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  Object.defineProperty(proto, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return height;
      const real: unknown = heightDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  return () => {
    if (widthDesc) Object.defineProperty(proto, "offsetWidth", widthDesc);
    if (heightDesc) Object.defineProperty(proto, "offsetHeight", heightDesc);
  };
}
