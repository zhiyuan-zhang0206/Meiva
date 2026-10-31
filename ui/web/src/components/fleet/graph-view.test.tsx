// meiva:note
// GraphView render tests: the force-directed fleet graph. The d3-force layout is a real, time-driven
// simulation (a black-box physics loop on requestAnimationFrame), so these tests do not pin exact
// coordinates: they drive the simulation far enough to settle node positions and assert the SVG the
// component renders off them — nodes and edges (including the message / fork / spawn variants),
// hover, select-on-click, open-on-double-click, and the loading / empty / error placeholders. Rows
// arrive through the fake transport, so no data hook is replaced. The pure structure helpers have
// their own tests in fleet-graph.test.ts.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

import { QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentRow,
  WireFleetGraph,
  WireFleetGraphEdge,
  WireFleetGraphNode,
} from "@/lib/contracts/types";

import { GraphView } from "./graph-view";

const navigate = vi.fn();

const { getAgentRoster } = vi.hoisted(() => ({ getAgentRoster: vi.fn() }));

import { AGENTS_QUERY_KEY } from "@/lib/agents/use-agents";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";
beforeEach(() => {
  resetTransport();
  transport.getAgentRoster.mockImplementation(getAgentRoster);
});


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

// meiva:note
// The test's door to the graph: the endpoint answers the graph the test states (or never settles, or
// fails, for the loading and error states), and the roster cache holds the agents the view resolves
// lineage against — production shape, never graph.nodes, which is live-only. Roster data reaches
// lineageById through the query cache; the default empty seed stops the mounted roster query from
// fetching in tests that do not exercise lineage (the fetch itself is asserted separately). The
// settings cache is seeded empty so the force sliders read defaults instead of fetching.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function renderGraph(
  ui: ReactElement,
  {
    graph,
    roster,
    fetch = "ok",
  }: {
    graph?: WireFleetGraph;
    roster?: { live?: AgentRow[]; terminated?: AgentRow[] };
    fetch?: "ok" | "pending" | "reject";
  } = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(AGENTS_QUERY_KEY, { agents: roster?.live ?? [], ancestors: roster?.terminated ?? [] });
  if (fetch === "pending") {
    transport.getFleetGraph.mockImplementation(neverSettles);
  } else if (fetch === "reject") {
    transport.getFleetGraph.mockRejectedValue(new Error("gateway unavailable"));
  } else if (graph !== undefined) {
    transport.getFleetGraph.mockResolvedValue(graph);
  }
  return renderWithProviders(ui, { queryClient: qc, settings: {}, wallNow: () => Date.now() });
}

// meiva:note
// A request that never settles is how a test holds the cold loading state: the fetch the query waits
// on stays unresolved, so the view keeps its first-load placeholder.
function neverSettles(): Promise<WireFleetGraph | undefined> {
  return new Promise<WireFleetGraph | undefined>(() => undefined);
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

function richGraph(): WireFleetGraph {
  const seen = "2026-06-17T00:00:00Z";
  const nodes: WireFleetGraphNode[] = [
    node(1, { label: "alpha", node_score: 50_000, total_tokens: 1_500_000 }),
    node(2, { status: "idling" }),
    node(3, {}),
    ...[4, 5, 6, 7, 8, 9].map((id) => node(id)),
  ];
  const edges: WireFleetGraphEdge[] = [
    edge(1, 2, "spawn"),
    edge(1, 3, "fork", { last_seen_at: seen }),
    ...[4, 5, 6, 7, 8, 9].map((id) =>
      edge(1, id, "message", { weight: 5, last_seen_at: seen }),
    ),
  ];
  return {
    nodes,
    edges,
    telemetry_stale: false,
    snapshot_at: null,
  };
}

// meiva:note
// The wire shape the endpoint returns: a fixture states the nodes and edges it cares about, and the
// two telemetry fields default here.
function graph(
  partial: Omit<WireFleetGraph, "telemetry_stale" | "snapshot_at"> & {
    telemetry_stale?: boolean;
    snapshot_at?: string | null;
  },
): WireFleetGraph {
  return { telemetry_stale: false, snapshot_at: null, ...partial };
}

beforeEach(() => {
  navigate.mockReset();
  getAgentRoster.mockReset();
});

afterEach(cleanup);

describe("GraphView", () => {
  it("renders nodes and edges; selects on click, opens on double-click", async () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));

    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />);

    // The simulation seeds positions asynchronously (tick -> rAF). Wait for the
    // node labels to land, which means positions populated and the SVG painted.
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(screen.getByText("9 nodes · 8 edges")).toBeTruthy();

    // Single-click selects the node (toggle on); it does NOT navigate.
    const group = label.closest("g")!;
    fireEvent.click(group);
    expect(navigate).not.toHaveBeenCalled();

    // Double-click opens the agent's conversation.
    fireEvent.doubleClick(group);
    expect(navigate).toHaveBeenCalledWith("/?agent_id=1");
  });

  it("has only a reset zoom control (no +/- buttons)", async () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />);
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(screen.getByLabelText("Reset zoom")).toBeTruthy();
    expect(screen.queryByLabelText("Zoom in")).toBeNull();
    expect(screen.queryByLabelText("Zoom out")).toBeNull();
  });

  it("explains status colors without terminated/offline or activity-score sizing legend", () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />);

    const legend = screen.getByLabelText("Agent graph legend");
    for (const label of ["Running", "Idling"]) {
      expect(legend.textContent).toContain(label);
    }
    for (const label of ["Terminated", "Offline"]) {
      expect(legend.textContent).not.toContain(label);
    }
    expect(legend.textContent).not.toContain("size = activity score");
  });

  it("shows the hover card with full node identity even when zoom hides labels", async () => {
    // The old native <title> tooltip (whose appearance the browser deferred)
    // is gone; the instant hover card carries the identity instead — visible
    // at any zoom level.
    transport.getFleetGraph.mockResolvedValue(graph({
        nodes: [node(1, { label: "alpha" }), node(2)],
        edges: [edge(1, 2, "spawn")],
      }));
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />,
    );
    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    // No native <title> remains in the SVG — the delayed tooltip is gone.
    expect(container.querySelectorAll("svg title").length).toBe(0);

    const svg = container.querySelector("svg")!;
    Object.defineProperty(svg, "createSVGPoint", {
      value: () => ({ x: 0, y: 0, matrixTransform: () => ({ x: 200, y: 200 }) }),
    });
    for (let i = 0; i < 8; i += 1) {
      fireEvent.wheel(svg, { deltaY: 100, clientX: 200, clientY: 200 });
    }

    await waitFor(() => expect(queryNodeLabel(1)).toBeNull());

    fireEvent.mouseEnter(group);
    const card = await screen.findByRole("tooltip");
    expect(card.textContent).toContain("alpha");
    expect(card.textContent).toContain("#1");
  });

  it("selecting a node focuses it (non-identity transform); reset restores identity", async () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />);

    // Node 2 has only one edge (→ node 1), so the neighbor bounding box is a
    // subset of the layout — zoom scale > 1 (non-identity).
    const label = await waitFor(() => getNodeLabel(2), { timeout: 4000 });
    // The content zoom layer is the only top-level <g> child of the <svg>.
    const zoomLayer = container.querySelector("svg > g")!;
    expect(zoomLayer.getAttribute("transform")).toBe("translate(0,0) scale(1)");

    // Clicking a node centers + zooms to include it and its neighbors.
    fireEvent.click(label.closest("g")!);
    await waitFor(() => {
      const t = zoomLayer.getAttribute("transform")!;
      expect(t).not.toBe("translate(0,0) scale(1)");
      expect((zoomLayer as SVGGElement).style.transition).toBe("transform 0.4s ease");
    });

    // Reset returns to the fit-to-content identity transform.
    fireEvent.click(screen.getByLabelText("Reset zoom"));
    await waitFor(() =>
      expect(zoomLayer.getAttribute("transform")).toBe("translate(0,0) scale(1)"),
    );
  });

  it("wheel zoom is not capped (scale can exceed the old 4x limit)", async () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    const { container } = renderGraph(<GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />);
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector("svg")!;
    const zoomLayer = container.querySelector("svg > g")!;
    // happy-dom drops WheelEvent client coordinates and its SVGPoint lacks
    // matrixTransform, so supply the cursor location requested by the event.
    Object.defineProperty(svg, "createSVGPoint", {
      value: () => ({ x: 0, y: 0, matrixTransform: () => ({ x: 200, y: 200 }) }),
    });
    fireEvent.wheel(svg, { deltaY: -100, clientX: 200, clientY: 200 });
    await waitFor(() => {
      const match = zoomLayer.getAttribute("transform")?.match(/translate\(([^,]+),([^)]+)\) scale\(([^)]+)\)/);
      expect(match).toBeTruthy();
      const scale = 2 ** 0.2;
      expect(Number(match![1])).toBeCloseTo(200 * (1 - scale));
      expect(Number(match![2])).toBeCloseTo(200 * (1 - scale));
      expect(Number(match![3])).toBeCloseTo(scale);
      expect((zoomLayer as SVGGElement).style.transition).toBe("none");
    });
    for (let i = 1; i < 12; i += 1) {
      fireEvent.wheel(svg, { deltaY: -100, clientX: 200, clientY: 200 });
    }

    await waitFor(() => {
      const scale = Number(zoomLayer.getAttribute("transform")?.match(/scale\(([^)]+)\)/)?.[1]);
      expect(scale).toBeGreaterThan(4);
    });
  });

  it("clicking an already-selected node is a no-op (does not deselect)", async () => {
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    const onSelect = vi.fn();

    // Render with agent 1 already selected.
    renderGraph(<GraphView selectedAgentId={1} onSelectAgent={onSelect} navigate={navigate} />);

    const label = await waitFor(() => getNodeLabel(1), { timeout: 4000 });
    const group = label.closest("g")!;

    // Click the already-selected node.
    fireEvent.click(group);
    // The click must not call onSelectAgent (no deselect, no re-select).
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("renders every edge solid and gray (message, spawn, and fork alike)", async () => {
    // User ruling 2026-08-05: no blue message edges — one muted gray for all.
    transport.getFleetGraph.mockResolvedValue(graph(richGraph()));
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    const lines = svg.querySelectorAll("line");
    expect(lines.length).toBe(8);
    for (const l of lines) {
      expect(l.getAttribute("class")).toBe("text-muted-foreground");
      expect(l.getAttribute("stroke-dasharray")).toBeNull();
    }
  });

  it("scales message-edge opacity across the graph's weight range", async () => {
    transport.getFleetGraph.mockResolvedValue(graph({
        nodes: [node(1), node(2), node(3)],
        edges: [
          edge(1, 2, "message", { weight: 1 }),
          edge(1, 3, "message", { weight: 2 }),
        ],
      }));
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    const opacities = Array.from(
      container.querySelectorAll('svg[aria-label="Fleet relationship graph"] line'),
      (line) => Number(line.getAttribute("stroke-opacity")),
    ).sort((a, b) => a - b);
    expect(opacities).toHaveLength(2);
    expect(opacities[0]).toBeCloseTo(0.545);
    expect(opacities[1]).toBeCloseTo(0.85);
  });

  it("drops terminated nodes and their edges before rendering (task #1104)", async () => {
    // User ruling 2026-08-09: terminated agents never appear in the agents
    // graph — the component filters by liveness FIRST (mirroring the sidebar
    // tree), so even a backend leak of a terminated row cannot paint it or
    // its edges. A live node whose lineage partner is terminated renders
    // without that edge.
    transport.getFleetGraph.mockResolvedValue(graph({
        nodes: [
          node(1, { label: "live" }),
          node(2, { label: "dead", status: "terminated" }),
          node(3, { label: "idling", status: "idling" }),
        ],
        edges: [
          edge(2, 1, "spawn"), // touches the terminated node — dropped
          edge(1, 3, "spawn"), // live-live — kept
        ],
      }));
    const { container } = renderGraph(
      <GraphView selectedAgentId={null} onSelectAgent={vi.fn()} navigate={navigate} />,
    );
    await waitFor(() => getNodeLabel(1), { timeout: 4000 });

    expect(queryNodeLabel(2)).toBeNull(); // terminated never renders
    expect(getNodeLabel(3)).toBeTruthy(); // idling is live
    expect(screen.getByText("2 nodes · 1 edges")).toBeTruthy();
    const svg = container.querySelector(
      'svg[aria-label="Fleet relationship graph"]',
    )!;
    expect(svg.querySelectorAll("line").length).toBe(1); // only live-live edge
  });
});
