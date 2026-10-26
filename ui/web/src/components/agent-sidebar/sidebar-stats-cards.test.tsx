import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SidebarModule from "@/lib/agents/sidebar";
import type { PluginStat, StatsDashboard } from "@/lib/contracts/types";
import type * as PluginStatsModule from "@/lib/plugins/plugin-stats";
import { bpMock, handlers, makeAgent, pushSpy, resetSidebarTestState, sortStore, state, StubAgentRow, StubButton, StubScrollArea, StubSpawnForm, wrap } from "./sidebar-test-mock";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushSpy }),
}));
vi.mock("@/lib/state/store", () => ({
  useStore: <T,>(selector: (s: unknown) => T): T => {
    const fake = {
      activeId: state.activeId,
      setActiveId: state.setActiveId,
      mobileSidebarOpen: state.mobileSidebarOpen,
      setMobileSidebarOpen: state.setMobileSidebarOpen,
      searchQuery: state.searchQuery,
      setSearchQuery: state.setSearchQuery,
    };
    return selector(fake);
  },
}));
vi.mock("@/lib/agents/sidebar", async () => {
  const actual = await vi.importActual<typeof SidebarModule>("@/lib/agents/sidebar");
  const React = await import("react");
  return {
    ...actual,
    useSidebarCollapsed: () => ({
      collapsed: state.sidebarCollapsed,
      setCollapsed: state.setSidebarCollapsed,
    }),
    useStatsDashboard: () => ({
      stats: state.stats,
      error: state.statsError,
      isFetching: state.statsFetching,
      refetch: state.statsRefetch,
    }),
    useStatsWindow: () => ({
      windowHours: state.statsWindowHours,
      setWindowHours: state.setStatsWindowHours,
    }),
    useSidebarViewMode: () => ({
      viewMode: state.sidebarViewMode,
      setViewMode: state.setSidebarViewMode,
    }),
    useSidebarSort: () => ({
      sort: React.useSyncExternalStore(
        sortStore.subscribe,
        () => sortStore.value ?? actual.SIDEBAR_SORT_DEFAULT,
        () => sortStore.value ?? actual.SIDEBAR_SORT_DEFAULT,
      ),
      setSort: (next: typeof actual.SIDEBAR_SORT_DEFAULT) => sortStore.set(next),
    }),
  };
});
vi.mock("@/lib/transport/api", () => ({
  api: {
    patchAgentLabel: vi.fn().mockResolvedValue(undefined),
    listAgents: vi.fn(() =>
      Promise.resolve({
        agents: state.agents.filter((a) => a.status === "terminated"),
        next_cursor: null,
      }),
    ),
  },
}));
vi.mock("@/lib/plugins/plugin-stats", async () => {
  const actual = await vi.importActual<typeof PluginStatsModule>("@/lib/plugins/plugin-stats");
  return {
    ...actual,
    usePluginStatCards: (values: PluginStat[] | undefined, now: Date) =>
      actual.buildPluginStatCards(state.pluginStatDeclarations, values, now),
  };
});
vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: { "display.show_terminated": state.showTerminated, ...state.userSettings },
    setSetting: state.setSetting,
    isLoading: false,
  }),
}));
vi.mock("@/components/agents/agent-row", () => ({
  AgentRow: StubAgentRow,
}));
vi.mock("@/components/agents/spawn-form", () => ({
  SpawnForm: StubSpawnForm,
}));
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: StubScrollArea,
}));
vi.mock("@/lib/layout/breakpoint", () => ({ useBreakpoint: () => bpMock() }));
vi.mock("@/components/ui/button", () => ({
  Button: StubButton,
}));
import { AgentSidebar } from "./index";
afterEach(cleanup);
beforeEach(resetSidebarTestState);

function openStats() {
  fireEvent.click(screen.getByLabelText("Statistics"));
}

describe("StatsCards tri-state (loading / data / error)", () => {
  it("stats=undefined + error=null → 6 cards show '—' placeholder", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText("Live agents")).toBeTruthy();
    expect(screen.getByText("Tokens")).toBeTruthy();
    expect(screen.getByText("Cache hit")).toBeTruthy();
    expect(screen.getByText("Cost")).toBeTruthy();
    expect(screen.getByText("Average turn time")).toBeTruthy();
    expect(screen.getByText("Warnings / errors")).toBeTruthy();
  });
  it("fetching without data shows an updating spinner and six skeleton values", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsFetching = true;
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByRole("status", { name: "Statistics are updating" })).toBeTruthy();
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(6);
    expect(screen.queryByText("—")).toBeNull();
  });
  it("stats has data → 6 cards show real numbers", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 19_588,
      errors: 41,
      alert_classes_active: 7,
      alert_classes_dismissed: 2,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByText("19.1k")).toBeTruthy();
    expect(screen.getByText("80.00%")).toBeTruthy();
    expect(screen.getByText("$1.23")).toBeTruthy();
    expect(screen.getByText("3s")).toBeTruthy();
    expect(screen.getByText("19588 / 41")).toBeTruthy();
    expect(screen.queryByText("19,629 events")).toBeNull();
    expect(screen.queryByText("7")).toBeNull();
  });
  it("unmetered cache shows N/A while tokens and cost remain visible", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 1,
      window_hours: 24,
      tokens: { input: 6000, output: 0, cache_read: 0, cache_hit_pct: null },
      cost_usd: 3,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("N/A")).toBeTruthy();
    expect(screen.getByText("6.0k")).toBeTruthy();
    expect(screen.getByText("$3.00")).toBeTruthy();
    expect(screen.queryByText("0.00%")).toBeNull();
  });
  it("dismissed classes do not replace the Warning / Error event totals", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 3,
      errors: 2,
      alert_classes_active: 0,
      alert_classes_dismissed: 5,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("3 / 2")).toBeTruthy();
    expect(screen.queryByText("5 events")).toBeNull();
    expect(screen.queryByText("All clear")).toBeNull();
  });
  it("keeps Warning / Error counts compact without opening an inline class list", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 1, output: 1, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 10_000,
      errors: 0,
      alert_classes_active: 1,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("10000 / 0")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Warnings \/ errors/ })).toBeNull();
    expect(screen.queryByText("disk_pressure")).toBeNull();
  });
  it("error without data shows retry and clicking it refetches", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getAllByText("!").length).toBeGreaterThanOrEqual(6);
    expect(document.querySelectorAll(".text-destructive").length).toBeGreaterThan(0);
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry.getAttribute("title")).toBe("stats endpoint 500");
    fireEvent.click(retry);
    expect(state.statsRefetch).toHaveBeenCalledOnce();
  });
  it("refetch failure keeps stale values and labels them with a warning", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1,
      avg_turn_seconds: 3,
      warnings: 2,
      errors: 1,
      alert_classes_active: 1,
      alert_classes_dismissed: 2,
      plugin_stats: [],
    };
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.queryByText("!")).toBeNull();
    expect(screen.getAllByTitle("stats endpoint 500").length).toBeGreaterThan(0);
  });
  it("tokens compact: < 1000 raw, < 1M as k, >= 1M as M", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 0,
      window_hours: 24,
      tokens: { input: 1_500_000, output: 0, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("1.5M")).toBeTruthy();
  });
  it("avg_turn_seconds=null → shows placeholder, not 'NaNs'", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 0,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});

describe("plugin stat cards", () => {
  function statsWith(pluginStats: StatsDashboard["plugin_stats"]): StatsDashboard {
    return {
      live_count: 7,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: pluginStats,
    };
  }
  it("a declared card renders the plugin's value and detail below the built-in grid", () => {
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
    openStats();
    expect(screen.getByText("Codex A")).toBeTruthy();
    expect(screen.getByText("Weekly reset 10-04 17:14 UTC+08:00")).toBeTruthy();
    expect(screen.getByText("52% remaining · 0 resets available")).toBeTruthy();
  });
  it("keeps each reset window and supporting detail in visible text", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.pluginStatDeclarations = [
      { plugin: "codex_usage", id: "codex-a", label: "Codex · zhiyuan0206" },
    ];
    state.stats = statsWith([
      {
        plugin: "codex_usage",
        id: "codex-a",
        value: "5h reset 09-29 02:00 UTC+08:00\nWeekly reset 10-04 17:14 UTC+08:00",
        detail: "5h 52% remaining · Weekly 68% remaining · 2 resets available",
        status: "ok",
        updated_at: new Date().toISOString(),
        updated_by: "macmini",
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("Codex · zhiyuan0206")).toBeTruthy();
    expect(screen.getByText(/5h reset 09-29 02:00 UTC\+08:00/).textContent).toBe(
      "5h reset 09-29 02:00 UTC+08:00\nWeekly reset 10-04 17:14 UTC+08:00",
    );
    expect(
      screen.getByText("5h 52% remaining · Weekly 68% remaining · 2 resets available"),
    ).toBeTruthy();
  });
  it("a declared card with no value row is the empty state; orphan values stay hidden", () => {
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
    openStats();
    expect(screen.getByText("Codex B")).toBeTruthy();
    expect(screen.getByText("71%")).toBeTruthy();
    expect(screen.queryByText("orphan-value")).toBeNull();
    expect(screen.getByTitle("No data yet")).toBeTruthy();
  });
  it("error status colors the value and keeps the plugin's message", () => {
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
    openStats();
    expect(screen.getByText("!").className).toContain("text-destructive");
    expect(screen.getByText("token revoked")).toBeTruthy();
  });
  it("a stale value is dimmed and its tooltip carries the age", () => {
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
        updated_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
        updated_by: null,
      },
    ]);
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("custom-42").closest("div")?.className).toContain("opacity-60");
    expect(screen.getByTitle("Updated 2h ago")).toBeTruthy();
  });
});

describe("StatsCards window selector", () => {
  it("hides stale windowed values while retaining the live-agent count", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      avg_turn_seconds: 3.14,
      warnings: 2,
      errors: 1,
      alert_classes_active: 1,
      alert_classes_dismissed: 2,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getAllByText("…")).toHaveLength(5);
    expect(screen.queryByText("19.1k")).toBeNull();
    expect(screen.queryByText("80.00%")).toBeNull();
    expect(screen.queryByText("$1.23")).toBeNull();
    expect(screen.queryByText("3s")).toBeNull();
    expect(screen.queryByText("Total 2")).toBeNull();
    expect(screen.getAllByTitle("Updating for 1h…")).toHaveLength(5);
  });
  it("renders all six window options with the persisted value selected", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 72;
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    const select = screen.getByLabelText<HTMLSelectElement>("Statistics window");
    expect(select.value).toBe("72");
    const labels = Array.from(select.options).map((o) => o.text);
    expect(labels).toEqual(["5m", "1h", "6h", "24h", "3d", "7d"]);
  });
  it("labels a retention-clamped selected window with the applied hours", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 168;
    state.stats = {
      live_count: 5,
      window_hours: 168,
      applied_window_hours: 84,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 1,
      avg_turn_seconds: 3,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    const select = screen.getByLabelText<HTMLSelectElement>("Statistics window");
    expect(select.selectedOptions[0].text).toBe("7d · 84h");
    expect(Array.from(select.options).find((option) => option.value === "72")?.text).toBe("3d");
  });
  it("changing the select calls setWindowHours with the numeric window value", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    const select = screen.getByLabelText("Statistics window");
    fireEvent.change(select, { target: { value: "6" } });
    expect(state.setStatsWindowHours).toHaveBeenCalledWith(6);
  });
  it("card labels render for each stat", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    wrap(<AgentSidebar {...handlers} />);
    openStats();
    expect(screen.getByText("Warnings / errors")).toBeTruthy();
    expect(screen.getByText("Live agents")).toBeTruthy();
    expect(screen.getByText("Cost")).toBeTruthy();
  });
});
