import { act, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BACK_VISIT_REVALIDATE_MAX } from "@/lib/layout/switch-budget";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgent,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  getSettings,
  liveFixture,
  putSetting,
  QueryClientProvider,
  render,
  resolveNotice,
  rosterFixture,
  setupInspectorTest,
  streamHandlers,
} from "./inspector-test-mock";

vi.mock("@/lib/transport/api", () => ({
  api: {
    getAgentInspectStatistics,
    getAgentInspectLive,
    getAgentInspectWidgets,
    getAgentRoster,
    getAgent,
    resolveNotice,
    putSetting,
    getSettings,
  },
}));

vi.mock("@/lib/transport/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: (
    onSystemEvent: (event: unknown) => void,
    onConnectionEvent: (event: { type: string }) => void,
  ) => {
    streamHandlers.system = onSystemEvent;
    streamHandlers.connection = onConnectionEvent;
  },
}));

import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel metrics and durations", () => {
  it("marks snapshot-less calls as N unpriced under the cost", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0073,
          unpriced_calls: 7,
          llm_calls: 8,
          tokens_in: 2_000_000,
          tokens_out: 0,
          tokens_cached: 2_000_000,
          tokens_reasoning: 0,
          cache_hit_pct: 100,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("$0.0073")).toBeTruthy();
    expect(screen.getByText("7 unpriced")).toBeTruthy();
  });

  it.each([
    [0, "—"],
    [42.54, "42.5"],
    [5_834, "5834.0"],
  ] as const)("formats TPS %s as %s", async (tps, expected) => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({ tps: { lm_stage_tps: tps, agent_lifecycle_tps: 0 } }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.queryByLabelText("Activity loading")).toBeNull());
    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getByText(expected)).toBeTruthy();
  });

  it("formats durations past 24h as Xd Yh (task #824): idle 24d 3h", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({
        activity: {
          active_seconds: 0,
          alive_seconds: 24 * 86_400 + 3 * 3_600,
          active_rate: 0,
          llm_seconds: 3_600,
          exec_seconds: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.queryByLabelText("Activity loading")).toBeNull());
    expect(screen.getByText("24d 3h")).toBeTruthy();
    expect(screen.getAllByText("1h").length).toBeGreaterThanOrEqual(2);
  });

  it("formats shell uptime past 24h as Xd Yh (task #824)", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        shells: [{ id: 9, name: "long-shell", created_at: null, uptime_seconds: 24 * 86_400 + 3 * 3_600 }],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("long-shell")).toBeTruthy());
    expect(screen.getByText("24d 3h")).toBeTruthy();
  });

  it("activity durations show em dashes when alive is 0 while TPS remains visible", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({ activity: { active_seconds: 0, alive_seconds: 0, active_rate: 0, llm_seconds: 0, exec_seconds: 0 } }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.queryByLabelText("Activity loading")).toBeNull());
    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getAllByText("—")).toHaveLength(3);
    expect(within(activitySection!).getByText("42.5")).toBeTruthy();
  });
});

describe("Cost cell billing presentation (user ruling 2026-10-28)", () => {
  // meiva:note
  // A subscription's label rides the amount in parentheses on one text run; a
  // metered (API) agent says nothing beyond the amount; an unresolved harness
  // keeps the amount alone. The assertions read the metric cell's structure, not
  // "the string appears somewhere": the cell is the label's parent (label span,
  // amount span, and only when the number needs a note a sub-line span), so the
  // old presentation's third row and a parenthetical split into its own element
  // both fail here.
  // meiva:link ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md
  const costCell = (): HTMLElement => {
    const cell = screen.getByText("Cost").parentElement;
    if (cell === null) throw new Error("the Cost metric rendered no cell");
    return cell;
  };

  const costRows = (): (string | null)[] =>
    [...costCell().children].map((child) => child.textContent);

  const activitySection = (): HTMLElement => {
    const section = screen.getByText("Activity").closest("section");
    if (section === null) throw new Error("the Activity section did not render");
    return section;
  };

  const costWith = (unpricedCalls: number) =>
    fixture({ cost: { ...fixture().cost!, unpriced_calls: unpricedCalls } });

  const awaitRosterHarness = (label: string) => screen.findByText(label);

  it("shows only the amount for a metered agent, never the billing mode", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    getAgentInspectStatistics.mockResolvedValue(costWith(0));
    render(<InspectorPanel agentId={1} />);
    await awaitRosterHarness("Meiva");

    expect(costRows()).toEqual(["Cost", "$0.4213"]);
    expect(activitySection().textContent).not.toMatch(/api/i);
  });

  it("keeps a subscription's label in the amount's text run, not on its own row", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "claude_code" }));
    getAgentInspectStatistics.mockResolvedValue(costWith(0));
    render(<InspectorPanel agentId={1} />);

    const amount = await screen.findByText("$0.4213 (Subscription)");
    expect(costRows()).toEqual(["Cost", "$0.4213 (Subscription)"]);
    expect(amount.childNodes).toHaveLength(1);
    expect(amount.childNodes[0].nodeType).toBe(Node.TEXT_NODE);
  });

  it("leaves the amount alone when the harness is unresolved", async () => {
    getAgentInspectStatistics.mockResolvedValue(costWith(0));
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("$0.4213");

    expect(costRows()).toEqual(["Cost", "$0.4213"]);
  });

  it("keeps unpriced calls on their own sub-line, beside the subscription amount", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "claude_code" }));
    getAgentInspectStatistics.mockResolvedValue(costWith(3));
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("$0.4213 (Subscription)");

    expect(costRows()).toEqual(["Cost", "$0.4213 (Subscription)", "3 unpriced"]);
  });

  it("keeps unpriced calls on their own sub-line for a metered agent too", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    getAgentInspectStatistics.mockResolvedValue(costWith(3));
    render(<InspectorPanel agentId={1} />);
    await awaitRosterHarness("Meiva");

    expect(costRows()).toEqual(["Cost", "$0.4213", "3 unpriced"]);
    expect(costCell().textContent).not.toMatch(/api/i);
  });
});

describe("Inspector read ownership", () => {
  it("reconnect storms do not restart statistics, and the scheduled refresh repairs them", async () => {
    vi.useFakeTimers();
    render(<InspectorPanel agentId={1} />);
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("$0.4213")).toBeTruthy();
    const changed = fixture();
    changed.cost!.cost_usd = 9;
    getAgentInspectStatistics.mockResolvedValue(changed);
    act(() => {
      for (let index = 0; index < 20; index++) streamHandlers.connection?.({ type: "open" });
    });
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(getAgentInspectStatistics).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(getAgentInspectStatistics).toHaveBeenCalledTimes(2);
    expect(screen.getByText("$9.0000")).toBeTruthy();
  });

  it("retains each visited agent's inspector entries for the window (back-switch cache, task #3894)", async () => {
    getAgentInspectLive.mockImplementation((id) =>
      Promise.resolve(liveFixture(id === 1 ? { agent_id: id } : { agent_id: id, shells: [] })),
    );
    getAgentInspectStatistics.mockImplementation((id) => Promise.resolve(fixture({ agent_id: id })));
    const client = createInspectorQueryClient();
    const view = renderWithBrowser(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
    await screen.findByText("dev-server");
    view.rerender(<QueryClientProvider client={client}><InspectorPanel agentId={2} /></QueryClientProvider>);
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledWith(2, expect.any(AbortSignal)));

    const inspectorEntries = () => client.getQueryCache().getAll().filter((query) => String(query.queryKey[0]).startsWith("agent-inspect"));
    await waitFor(() => expect(inspectorEntries()).toHaveLength(6));
    expect(new Set(inspectorEntries().map((query) => query.queryKey[1]))).toEqual(new Set([1, 2]));

    view.rerender(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
    expect(screen.getByText("dev-server")).toBeTruthy();

    view.unmount();
    expect(inspectorEntries()).toHaveLength(6);
  });

  it("a back-switch revalidates at most the two live reads and never shows a cold skeleton", async () => {
    getAgentInspectLive.mockImplementation((id) =>
      Promise.resolve(liveFixture(id === 1 ? { agent_id: id } : { agent_id: id, shells: [] })),
    );
    getAgentInspectStatistics.mockImplementation((id) => Promise.resolve(fixture({ agent_id: id })));
    const client = createInspectorQueryClient();
    const view = renderWithBrowser(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
    await screen.findByText("dev-server");
    view.rerender(<QueryClientProvider client={client}><InspectorPanel agentId={2} /></QueryClientProvider>);
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledWith(2, expect.any(AbortSignal)));

    const liveCalls = (id: number) =>
      getAgentInspectLive.mock.calls.filter(([calledId]) => calledId === id).length;
    const statsCalls = (id: number) =>
      getAgentInspectStatistics.mock.calls.filter(([calledId]) => calledId === id).length;
    const widgetCalls = (id: number) =>
      getAgentInspectWidgets.mock.calls.filter(([calledId]) => calledId === id).length;
    expect(widgetCalls(1)).toBe(1);
    const liveBefore = liveCalls(1);
    const statsBefore = statsCalls(1);

    view.rerender(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
    expect(screen.getByText("dev-server")).toBeTruthy();
    expect(screen.queryByLabelText("Persistent shells loading")).toBeNull();

    await waitFor(() => expect(liveCalls(1)).toBe(liveBefore + 1));
    const revalidated = liveCalls(1) - liveBefore + (statsCalls(1) - statsBefore);
    expect(revalidated).toBeLessThanOrEqual(BACK_VISIT_REVALIDATE_MAX);
    expect(widgetCalls(1)).toBe(1);
  });
});

describe("persisted metric evidence is internal (task #3869)", () => {
  it("renders unavailable metrics without manufacturing zero cost, throughput, or a verdict", async () => {
    const data = fixture({
      cost: null,
      stats: null,
      tps: null,
      activity: null,
    });
    data.metadata.cost = { availability: "unavailable", sources: [] };
    data.metadata.activity = { availability: "unavailable", sources: [] };
    data.metadata.lifecycle = { availability: "unavailable", sources: [] };
    getAgentInspectStatistics.mockResolvedValue(data);
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Activity");
    await waitFor(() => expect(getAgentInspectStatistics).toHaveBeenCalled());
    expect(screen.queryByText("$0.0000")).toBeNull();
    expect(screen.queryByText("0.00 tok/s")).toBeNull();
    expect(screen.queryByText("No recorded data is available for this window.")).toBeNull();
    expect(screen.queryByText("Historical coverage is incomplete")).toBeNull();
  });

  it("keeps the recorded amount while never rendering the historical-coverage verdict", async () => {
    const data = fixture();
    data.metadata.cost = {
      availability: "partial",
      sources: ["historical_daily_costs"],
    };
    getAgentInspectStatistics.mockResolvedValue(data);
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("$0.4213");
    expect(screen.queryByText("Historical coverage is incomplete")).toBeNull();
    expect(screen.queryByText(/Latest recorded observation:/)).toBeNull();
  });
});

describe("internal duration evidence never renders (task #3869)", () => {
  it.each([
    ["one_second_buckets", "Historical durations use one-second buckets."],
    ["mixed", "Durations combine exact values and historical one-second buckets."],
  ] as const)("hides the %s precision note while activity stays visible", async (precision, label) => {
    const data = fixture();
    data.metadata.turns.duration_precision = precision;
    getAgentInspectStatistics.mockResolvedValue(data);
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("42.5");
    expect(screen.queryByText(label)).toBeNull();
  });

  it("hides the retained-durations note", async () => {
    const data = fixture();
    data.metadata.turns.retained_unapplied_sources = ["historical_archive_distribution"];
    data.metadata.turns.reason = "archive_precision_unattributed";
    getAgentInspectStatistics.mockResolvedValue(data);
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("42.5");
    expect(
      screen.queryByText("Older exact durations are retained, but cannot be assigned to this window."),
    ).toBeNull();
    expect(screen.queryByText("historical_archive_distribution")).toBeNull();
  });

  it("hides the missing-durations note while throughput stays unknown", async () => {
    const data = fixture({ tps: { lm_stage_tps: null, agent_lifecycle_tps: null } });
    data.metadata.turns.reason = "missing_turn_durations";
    getAgentInspectStatistics.mockResolvedValue(data);
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Activity");
    await waitFor(() => expect(getAgentInspectStatistics).toHaveBeenCalled());
    expect(
      screen.queryByText("Some recorded turns have no duration; throughput is unavailable."),
    ).toBeNull();
    expect(screen.queryByText("42.5")).toBeNull();
  });
});
