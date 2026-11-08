import { act, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentCost } from "@/lib/contracts/types";
import { BACK_VISIT_REVALIDATE_MAX } from "@/lib/layout/switch-budget";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  liveFixture,
  QueryClientProvider,
  render,
  rosterFixture,
  setupInspectorTest,
  streamHandlers,
} from "./inspector-test-mock";




import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel metrics and durations", () => {
  it("marks snapshot-less calls as N unpriced under the cost", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0073,
          cost_usd_api: 0.0073,
          cost_usd_subscription: 0,
          unpriced_calls: 7,
          unpriced_calls_api: 7,
          unpriced_calls_subscription: 0,
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

describe("Cost blocks by billing mode (user ruling 2026-11-08)", () => {
  // meiva:note
  // One block per billing mode, each naming the mode in its label and the amount
  // carrying no parenthetical: `Cost (subscription)` is the list value a
  // subscription covered, `Cost (API)` the metered money owed. The assertions read
  // the metric cell's own children (label, amount, and only when the mode has
  // unpriced calls a sub-line), so a suffix riding the amount or a stray third row
  // fails here.
  // meiva:link ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md
  const metricCell = (label: string): HTMLElement => {
    const cell = screen.getByText(label).parentElement;
    if (cell === null) throw new Error(`the ${label} metric rendered no cell`);
    return cell;
  };

  const cellRows = (label: string): (string | null)[] =>
    [...metricCell(label).children].map((child) => child.textContent);

  const activitySection = (): HTMLElement => {
    const section = screen.getByText("Activity").closest("section");
    if (section === null) throw new Error("the Activity section did not render");
    return section;
  };

  const apiCost = (unpricedCalls: number) =>
    fixture({
      cost: {
        ...fixture().cost!,
        cost_usd_api: fixture().cost!.cost_usd,
        unpriced_calls_api: unpricedCalls,
      },
    });

  const subscriptionCost = (unpricedCalls: number) =>
    fixture({
      cost: {
        ...fixture().cost!,
        cost_usd_api: 0,
        cost_usd_subscription: fixture().cost!.cost_usd,
        unpriced_calls_api: 0,
        unpriced_calls_subscription: unpricedCalls,
      },
    });

  it("names the mode in the label and keeps the amount alone on its own run", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "claude_code" }));
    getAgentInspectStatistics.mockResolvedValue(subscriptionCost(0));
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Cost (subscription)");

    expect(cellRows("Cost (subscription)")).toEqual(["Cost (subscription)", "$0.4213"]);
    expect(activitySection().textContent).not.toMatch(/\(Subscription\)/);
    expect(screen.queryByText("Cost (API)")).toBeNull();
  });

  it("keeps a metered agent's unpriced calls on the API block's own sub-line", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    getAgentInspectStatistics.mockResolvedValue(apiCost(3));
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Meiva");

    expect(cellRows("Cost (API)")).toEqual(["Cost (API)", "$0.4213", "3 unpriced"]);
    expect(screen.queryByText("Cost (subscription)")).toBeNull();
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
    changed.cost!.cost_usd_api = 9;
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

  it("a back-switch revalidates at most the two live reads and never shows a cold pending tell", async () => {
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
    expect(screen.queryByLabelText("Liveness loading")).toBeNull();

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
    await waitFor(() => expect(screen.queryByLabelText("Activity loading")).toBeNull());
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
    await waitFor(() => expect(screen.queryByLabelText("Activity loading")).toBeNull());
    expect(
      screen.queryByText("Some recorded turns have no duration; throughput is unavailable."),
    ).toBeNull();
    expect(screen.queryByText("42.5")).toBeNull();
  });
});


describe("InspectorPanel cost blocks by billing mode (user ruling 2026-11-08)", () => {
  // meiva:note
  // `cost_usd_api` and `cost_usd_subscription` are the same agent's spend split by the billing
  // mode of the loop that ran each call. A block shows only when its amount is above zero, so a
  // mode the agent never ran — and a mode whose calls were all unpriced — has no block; the one
  // that does show prints the amount alone, with no `(Subscription)` suffix riding beside it.
  const cost = (overrides: Partial<AgentCost>) => fixture({
    cost: {
      cost_usd: 0,
      cost_usd_api: 0,
      cost_usd_subscription: 0,
      unpriced_calls: 0,
      unpriced_calls_api: 0,
      unpriced_calls_subscription: 0,
      llm_calls: 0,
      tokens_in: 0,
      tokens_out: 0,
      tokens_cached: 0,
      tokens_reasoning: 0,
      cache_hit_pct: 0,
      ...overrides,
    },
  });

  it("renders only the API block when the agent never ran a subscription harness", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      cost({ cost_usd: 1.25, cost_usd_api: 1.25, llm_calls: 3 }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("$1.2500")).toBeTruthy());
    expect(screen.getByText("Cost (API)")).toBeTruthy();
    expect(screen.getByTestId("inspector-cost-api")).toBeTruthy();
    expect(screen.queryByText("Cost (subscription)")).toBeNull();
    expect(screen.queryByTestId("inspector-cost-subscription")).toBeNull();
  });

  it("renders only the subscription block when every call ran on a subscription harness", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      cost({ cost_usd: 2.5, cost_usd_subscription: 2.5, llm_calls: 4 }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("$2.5000")).toBeTruthy());
    expect(screen.getByText("Cost (subscription)")).toBeTruthy();
    expect(screen.getByTestId("inspector-cost-subscription")).toBeTruthy();
    expect(screen.queryByText("Cost (API)")).toBeNull();
    expect(screen.queryByTestId("inspector-cost-api")).toBeNull();
    expect(screen.queryByText(/\(Subscription\)/)).toBeNull();
  });

  it("renders both blocks when the agent's history covers both modes", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      cost({
        cost_usd: 1.75,
        cost_usd_api: 0.25,
        cost_usd_subscription: 1.5,
        llm_calls: 5,
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("$0.2500")).toBeTruthy());
    expect(screen.getByText("Cost (API)")).toBeTruthy();
    expect(screen.getByText("Cost (subscription)")).toBeTruthy();
    expect(screen.getByText("$1.5000")).toBeTruthy();
  });

  it("hides a zero-amount block even when that mode logged unpriced calls", async () => {
    // meiva:note
    // The amount is the whole test: a mode that spent nothing has no block, and the panel never
    // answers "$0.0000" to a question about cost (user ruling 2026-11-08, restated by 405). The
    // visible mode keeps its own unpriced note on the sub-line.
    getAgentInspectStatistics.mockResolvedValue(
      cost({
        cost_usd: 0.5,
        cost_usd_api: 0.5,
        unpriced_calls: 2,
        unpriced_calls_api: 1,
        unpriced_calls_subscription: 1,
        llm_calls: 2,
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Cost (API)")).toBeTruthy());
    expect(screen.getByText("$0.5000")).toBeTruthy();
    expect(screen.getByText("1 unpriced")).toBeTruthy();
    expect(screen.queryByText("Cost (subscription)")).toBeNull();
    expect(screen.queryByTestId("inspector-cost-subscription")).toBeNull();
    expect(screen.queryByText("$0.0000")).toBeNull();
  });
});
