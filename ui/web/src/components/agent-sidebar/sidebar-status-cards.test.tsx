import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StatsDashboard } from "@/lib/contracts/types";
import { transport } from "@/test-support/fake-api";
import { handlers, makeAgent, resetSidebarTestState, SCENARIO_NOW, state, wrap } from "./sidebar-test-mock";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { AgentSidebar } from "./index";
afterEach(() => { cleanup(); vi.useRealTimers(); });
beforeEach(resetSidebarTestState);

function openStatus() {
  fireEvent.click(screen.getByLabelText("Status"));
}

const ZERO_ALERTS = { unresolved: 0, critical: 0, error: 0, warning: 0 };

function makeStats(overrides: Partial<StatsDashboard> = {}): StatsDashboard {
  return {
    live_count: 5,
    window_hours: 24,
    tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
    cost_usd: 25.4,
    subscription_cost_usd: 0,
    usage_unpriced_calls: 0,
    usage_incomplete_reports: 0,
    openai_cost_usd: 12.34,
    anthropic_cost_usd: 5.67,
    google_cost_usd: 1.15,
    alerts: ZERO_ALERTS,
    plugin_stats: [],
    ...overrides,
  };
}

describe("StatusCards tri-state (loading / data / error)", () => {
  it("fetching without data shows a spinner in every value cell and no skeleton bar", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsFetching = true;
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByRole("status", { name: "Status is updating" })).toBeTruthy();
    // meiva:note
    // Eight placeholder cells (live agents + seven windowed usage cards) each show a
    // spinner; none of them keeps a placeholder bar shaped like a value.
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(0);
    expect(document.querySelectorAll(".animate-spin").length).toBeGreaterThanOrEqual(8);
    expect(screen.queryByText("—")).toBeNull();
  });

  it("stats has data → live agents and 7 windowed usage cards show real numbers without alerts or diagnostics", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      alerts: { unresolved: 3, critical: 1, error: 1, warning: 1 },
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("5")).toBeTruthy();
    expect(await screen.findByText("19.1k")).toBeTruthy();
    expect(await screen.findByText("80.00%")).toBeTruthy();
    expect(await screen.findByText("$25.40")).toBeTruthy();
    expect(await screen.findByText("$12.34")).toBeTruthy();
    expect(await screen.findByText("$5.67")).toBeTruthy();
    expect(await screen.findByText("$1.15")).toBeTruthy();
    const googleCard = screen.getByText("Google API").closest("div");
    expect(googleCard?.getAttribute("title")).toBe(
      "Metered Google API spend over the past 24h (excludes subscription harnesses): $1.15",
    );
    const usageGrid = googleCard?.parentElement;
    expect(usageGrid?.className).toContain("grid-cols-2");
    expect(usageGrid?.children).toHaveLength(7);
    expect(document.querySelector(".grid-cols-3")).toBeNull();
    expect(screen.queryByTestId("status-live-alerts")).toBeNull();
    expect(screen.queryByTestId("status-diagnostics-card")).toBeNull();
    expect(screen.queryByText("Alerts")).toBeNull();
    expect(screen.queryByText("Diagnostics")).toBeNull();
    expect(screen.queryByText("Warnings / errors")).toBeNull();
    expect(screen.queryByRole("button", { name: /dismiss/i })).toBeNull();
  });

  it("unmetered cache shows N/A while tokens and cost remain visible", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      live_count: 1,
      tokens: { input: 6000, output: 0, cache_read: 0, cache_hit_pct: null },
      cost_usd: 3,
      openai_cost_usd: 1.5,
      anthropic_cost_usd: 0.5,
      google_cost_usd: 0.25,
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("N/A")).toBeTruthy();
    expect(await screen.findByText("6.0k")).toBeTruthy();
    expect(await screen.findByText("$3.00")).toBeTruthy();
    expect(screen.queryByText("0.00%")).toBeNull();
  });

  it("error without data shows retry and clicking it refetches", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect((await screen.findAllByText("!")).length).toBe(8);
    expect(document.querySelectorAll(".text-destructive").length).toBeGreaterThan(0);
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(retry.getAttribute("title")).toBe("stats endpoint 500");
    const reads = transport.getStatsDashboard.mock.calls.length;
    fireEvent.click(retry);
    expect(transport.getStatsDashboard.mock.calls.length).toBe(reads + 1);
  });

  it("a failing background read keeps the last numbers and labels them with the warning", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1,
      openai_cost_usd: 0.5,
      anthropic_cost_usd: 0.25,
      google_cost_usd: 0.1,
    });
    state.statsError = new Error("stats endpoint 500");
    vi.useFakeTimers();
    try {
      wrap(<AgentSidebar {...handlers} />);
      openStatus();
      await vi.advanceTimersByTimeAsync(0);
      expect(screen.getAllByText("5").length).toBeGreaterThan(0);
      await vi.advanceTimersByTimeAsync(30_000);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(screen.getByRole("img", { name: "stats endpoint 500" })).toBeTruthy();
      expect(screen.getAllByText("5").length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("tokens compact: < 1000 raw, < 1M as k, >= 1M as M", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      live_count: 0,
      tokens: { input: 1_500_000, output: 0, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      openai_cost_usd: 0,
      anthropic_cost_usd: 0,
      google_cost_usd: 0,
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("1.5M")).toBeTruthy();
  });

  it("null provider cost slices (unavailable model catalog) show placeholder instead of $0.00", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      live_count: 2,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 1.5,
      openai_cost_usd: null,
      anthropic_cost_usd: null,
      google_cost_usd: null,
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("$1.50")).toBeTruthy();
    expect(await screen.findAllByText("—")).toHaveLength(3);
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});

describe("plugin status cards", () => {
  function statsWith(pluginStats: StatsDashboard["plugin_stats"]): StatsDashboard {
    return makeStats({
      live_count: 7,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      openai_cost_usd: 0,
      anthropic_cost_usd: 0,
      google_cost_usd: 0,
      plugin_stats: pluginStats,
    });
  }
  it("renders every structured deadline with relative text and full-time tooltip", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [{ plugin: "meiva_usage", id: "quota", label: "Quota" }];
    state.stats = statsWith([{
      plugin: "meiva_usage", id: "quota", value: "40% left", detail: "5h 80% left · 7d 40% left",
      status: "ok", updated_at: new Date(SCENARIO_NOW).toISOString(), updated_by: "macmini",
      resets: [
        { label: "5h", resets_at: "2026-05-20T08:02:00+08:00" },
        { label: "7d", resets_at: new Date(SCENARIO_NOW + 86400_000 * 2).toISOString() },
        { label: "expired", resets_at: new Date(SCENARIO_NOW).toISOString() },
        { label: "soon", resets_at: new Date(SCENARIO_NOW + 30_000).toISOString() },
        { label: "1d+", resets_at: new Date(SCENARIO_NOW + (47 * 60 + 59) * 60_000).toISOString() },
      ],
    }]);
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let clock = SCENARIO_NOW;
    wrap(<WallClockProvider wallNow={() => clock}><AgentSidebar {...handlers} /></WallClockProvider>);
    openStatus();
    expect(await screen.findByText("5h · resets in 2m")).toBeTruthy();
    expect(await screen.findByText("7d · resets in 2d")).toBeTruthy();
    expect(await screen.findByText("expired · awaiting refresh")).toBeTruthy();
    expect(await screen.findByText("soon · resets soon")).toBeTruthy();
    expect(await screen.findByText("1d+ · resets in 1d 23h")).toBeTruthy();
    expect(screen.getByText("5h · resets in 2m").getAttribute("title")).toMatch(/2026-05-20/);
    try {
      clock += 120_000;
      act(() => { vi.advanceTimersByTime(1000); });
      expect(screen.getByText("5h · awaiting refresh")).toBeTruthy();
    } finally { vi.useRealTimers(); }
  });
  it.each([true, false])("keeps retained-error age and deadlines coherent (stale=%s)", async (stale) => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [{ plugin: "meiva_usage", id: "snapshot", label: "Snapshot" }];
    state.stats = statsWith([{
      plugin: "meiva_usage", id: "snapshot", value: "40% left", detail: stale ? "network down" : "healthy",
      status: stale ? "error" : "ok", updated_at: new Date(SCENARIO_NOW - (stale ? 3600_000 : 0)).toISOString(),
      updated_by: "macmini", resets: [
        { label: "5h", resets_at: new Date(SCENARIO_NOW + 120_000).toISOString() },
        { label: "7d", resets_at: new Date(SCENARIO_NOW - 120_000).toISOString() },
      ],
    }]);
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const value = await screen.findByText("40% left");
    expect(value.parentElement?.className.includes("opacity-60")).toBe(stale);
    expect(value.className.includes("text-destructive")).toBe(stale);
    expect(await screen.findByTitle(stale ? "Updated 1h ago" : "Updated just now")).toBeTruthy();
    expect(await screen.findByText("5h · resets in 2m")).toBeTruthy();
    expect(await screen.findByText("7d · awaiting refresh")).toBeTruthy();
  });
  it("a declared card renders the plugin's value and detail in the unwindowed section above Usage", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [{ plugin: "codex_usage", id: "codex-a", label: "Codex A" }];
    state.stats = statsWith([
      {
        plugin: "codex_usage",
        id: "codex-a",
        value: "Weekly reset 10-04 17:14 UTC+08:00",
        detail: "52% remaining · 0 resets available",
        status: "ok",
        updated_at: new Date().toISOString(),
        updated_by: "macmini",
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const codex = await screen.findByText("Codex A");
    const usageHeading = await screen.findByText("Usage");
    expect(codex.compareDocumentPosition(usageHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await screen.findByText("Weekly reset 10-04 17:14 UTC+08:00")).toBeTruthy();
    expect(await screen.findByText("52% remaining · 0 resets available")).toBeTruthy();
  });
  it("a declared card with no value row is the empty state; orphan values stay hidden", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [
      { plugin: "codex_usage", id: "codex-b", label: "Codex B" },
      { plugin: "codex_usage", id: "codex-a", label: "Codex A" },
    ];
    state.stats = statsWith([
      {
        plugin: "codex_usage",
        id: "codex-a",
        value: "71%",
        detail: null,
        status: "ok",
        updated_at: new Date().toISOString(),
        updated_by: null,
      },
      {
        plugin: "codex_usage",
        id: "retired",
        value: "orphan-value",
        detail: null,
        status: "ok",
        updated_at: new Date().toISOString(),
        updated_by: null,
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("Codex B")).toBeTruthy();
    expect(await screen.findByText("71%")).toBeTruthy();
    expect(screen.queryByText("orphan-value")).toBeNull();
    expect(await screen.findByTitle("No data yet")).toBeTruthy();
  });
  it("error status colors the value and keeps the plugin's message", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [{ plugin: "codex_usage", id: "codex-b", label: "Codex B" }];
    state.stats = statsWith([
      {
        plugin: "codex_usage",
        id: "codex-b",
        value: "!",
        detail: "token revoked",
        status: "error",
        updated_at: new Date().toISOString(),
        updated_by: null,
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect((await screen.findByText("!")).className).toContain("text-destructive");
    expect(await screen.findByText("token revoked")).toBeTruthy();
  });
  it("a stale value is dimmed and its tooltip carries the age", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [
      { plugin: "deepseek_balance", id: "balance", label: "Balance" },
    ];
    state.stats = statsWith([
      {
        plugin: "deepseek_balance",
        id: "balance",
        value: "custom-42",
        detail: null,
        status: "ok",
        updated_at: new Date(SCENARIO_NOW - 2 * 60 * 60 * 1000).toISOString(),
        updated_by: null,
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect((await screen.findByText("custom-42")).closest("div")?.className).toContain("opacity-60");
    expect(await screen.findByTitle("Updated 2h ago")).toBeTruthy();
  });
});

describe("StatusCards window selector", () => {
  it("hides stale windowed values while retaining live agents and plugin cards", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    state.pluginStatDeclarations = [{ plugin: "deepseek_balance", id: "bal", label: "DeepSeek" }];
    state.stats = makeStats({
      window_hours: 24,
      cost_usd: 10.5,
      openai_cost_usd: 4.2,
      anthropic_cost_usd: 3.1,
      google_cost_usd: 1.4,
      alerts: { unresolved: 2, critical: 1, error: 1, warning: 0 },
      plugin_stats: [
        {
          plugin: "deepseek_balance",
          id: "bal",
          value: "$42.80",
          detail: null,
          status: "ok",
          updated_at: new Date().toISOString(),
          updated_by: null,
        },
      ],
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("5")).toBeTruthy();
    expect(await screen.findByText("$42.80")).toBeTruthy();
    expect(await screen.findAllByText("…")).toHaveLength(7);
    expect(await screen.findAllByTitle("Updating for 1h…")).toHaveLength(7);
  });

  it("renders all seven window options including 30d with the persisted value selected", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 720;
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const select = await screen.findByLabelText<HTMLSelectElement>("Status time window");
    expect(select.value).toBe("720");
    const labels = Array.from(select.options).map((o) => o.text);
    expect(labels).toEqual(["5m", "1h", "6h", "24h", "3d", "7d", "30d"]);
  });

  it("labels a retention-clamped selected window with the applied hours", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 168;
    state.stats = makeStats({
      window_hours: 168,
      applied_window_hours: 84,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 1,
      openai_cost_usd: 0.5,
      anthropic_cost_usd: 0.2,
      google_cost_usd: 0.1,
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const select = await screen.findByLabelText<HTMLSelectElement>("Status time window");
    await waitFor(() => expect(select.selectedOptions[0].text).toBe("7d · 84h"));
    expect(Array.from(select.options).find((option) => option.value === "72")?.text).toBe("3d");
  });

  it("changing the select calls setWindowHours with the numeric window value", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const select = await screen.findByLabelText("Status time window");
    fireEvent.change(select, { target: { value: "720" } });
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("display.stats_window_hours", 720),
    );
  });

  it.each([
    { cost: 0, subscription: 0, api: "$0.00", sub: "$0.00", case: "both halves zero" },
    { cost: 12.5, subscription: 0, api: "$12.50", sub: "$0.00", case: "subscription half zero" },
    { cost: 0, subscription: 3.75, api: "$0.00", sub: "$3.75", case: "API half zero" },
    { cost: 10.5, subscription: 2.25, api: "$10.50", sub: "$2.25", case: "both halves non-zero" },
  ])("cost cards split API from subscription when $case", async ({ cost, subscription, api, sub }) => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = makeStats({
      live_count: 1,
      tokens: { input: 1000, output: 100, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: cost,
      subscription_cost_usd: subscription,
      openai_cost_usd: null,
      anthropic_cost_usd: null,
      google_cost_usd: null,
    });
    wrap(<AgentSidebar {...handlers} />);
    openStatus();

    const apiCard = (await screen.findByText("Cost (API)")).closest("div");
    const subCard = screen.getByText("Cost (subscription)").closest("div");
    await waitFor(() => expect(apiCard?.textContent).toBe(`Cost (API)${api}`));
    expect(subCard?.textContent).toBe(`Cost (subscription)${sub}`);
    expect(apiCard?.getAttribute("title")).toContain(`: ${api}`);
    expect(subCard?.getAttribute("title")).toContain("not money owed");
    expect(subCard?.getAttribute("title")).toContain(`: ${sub}`);
  });

  it("keeps Live agents above Usage and renders only the 7 Usage cards", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const live = await screen.findByText("Live agents");
    const usage = await screen.findByText("Usage");
    expect(live.compareDocumentPosition(usage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await screen.findByText("Tokens")).toBeTruthy();
    expect(await screen.findByText("Cache hit")).toBeTruthy();
    expect(await screen.findByText("Cost (API)")).toBeTruthy();
    expect(await screen.findByText("Cost (subscription)")).toBeTruthy();
    expect(await screen.findByText("OpenAI API")).toBeTruthy();
    expect(await screen.findByText("Anthropic API")).toBeTruthy();
    expect(await screen.findByText("Google API")).toBeTruthy();
    expect(screen.queryByTestId("status-live-alerts")).toBeNull();
    expect(screen.queryByTestId("status-diagnostics-card")).toBeNull();
    expect(screen.queryByText("Alerts")).toBeNull();
    expect(screen.queryByText("Diagnostics")).toBeNull();
    expect(screen.queryByText("Warnings / errors")).toBeNull();
  });
});
