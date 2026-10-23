import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  render as rtlRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as TimeFormat from "@/lib/format/time";
import { BACK_VISIT_REVALIDATE_MAX } from "@/lib/layout/switch-budget";
import {
  cleanupInspectorTest,
  fixture,
  getAgent,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  isLargeMock,
  listPages,
  liveFixture,
  panelState,
  render,
  resolveNotice,
  setInspectorHours,
  setupInspectorTest,
  streamHandlers,
  toggle,
} from "./inspector-test-harness";

vi.mock("@/lib/transport/api", () => ({
  api: {
    getAgentInspectStatistics,
    getAgentInspectLive,
    getAgentInspectWidgets,
    getAgentRoster,
    getAgent,
    listPages,
    resolveNotice,
    getSettings: vi.fn(() => Promise.resolve({ settings: [] })),
  },
}));

vi.mock("@/lib/format/time", async (importOriginal) => {
  const actual = await importOriginal<typeof TimeFormat>();
  return { ...actual, formatUpcoming: vi.fn(actual.formatUpcoming) };
});

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

vi.mock("@/lib/inspector/inspector-panel-store", async () => {
  const React = await import("react");
  return {
    useInspectorOpen: () => ({ open: panelState.open, toggle }),
    useInspectorHours: () => {
      const [hours, setHours] = React.useState(panelState.hours);
      return {
        inspectorHours: hours,
        setInspectorHours: (next: number | null) => {
          setInspectorHours(next);
          setHours(next);
        },
      };
    },
  };
});

vi.mock("@/lib/layout/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
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
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
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
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(<QueryClientProvider client={client}><InspectorPanel agentId={1} /></QueryClientProvider>);
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
