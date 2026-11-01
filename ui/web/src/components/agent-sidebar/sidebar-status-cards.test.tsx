import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StatsDashboard } from "@/lib/contracts/types";
import { transport } from "@/test-support/fake-api";
import { handlers, makeAgent, resetSidebarTestState, SCENARIO_NOW, state, wrap } from "./sidebar-test-mock";
import { AgentSidebar } from "./index";
afterEach(cleanup);
beforeEach(resetSidebarTestState);

function openStatus() {
  fireEvent.click(screen.getByLabelText("Status"));
}

describe("StatusCards tri-state (loading / data / error)", () => {
  it("fetching without data shows an updating spinner and seven skeleton values", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsFetching = true;
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByRole("status", { name: "Status is updating" })).toBeTruthy();
    expect(document.querySelectorAll(".animate-pulse")).toHaveLength(7);
    expect(screen.queryByText("—")).toBeNull();
  });
  it("stats has data → live count and 6 windowed cards show real numbers", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 25.4,
      openai_cost_usd: 12.34,
      anthropic_cost_usd: 5.67,
      warnings: 19_588,
      errors: 41,
      alert_classes_active: 7,
      alert_classes_dismissed: 2,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("5")).toBeTruthy();
    expect(await screen.findByText("19.1k")).toBeTruthy();
    expect(await screen.findByText("80.00%")).toBeTruthy();
    expect(await screen.findByText("$25.40")).toBeTruthy();
    expect(await screen.findByText("$12.34")).toBeTruthy();
    expect(await screen.findByText("$5.67")).toBeTruthy();
    expect(await screen.findByText("19588 / 41")).toBeTruthy();
    expect(screen.queryByText("Average turn time")).toBeNull();
    expect(screen.queryByText("19,629 events")).toBeNull();
    expect(screen.queryByText("7")).toBeNull();
  });
  it("unmetered cache shows N/A while tokens and cost remain visible", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 1,
      window_hours: 24,
      tokens: { input: 6000, output: 0, cache_read: 0, cache_hit_pct: null },
      cost_usd: 3,
      openai_cost_usd: 1.5,
      anthropic_cost_usd: 0.5,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("N/A")).toBeTruthy();
    expect(await screen.findByText("6.0k")).toBeTruthy();
    expect(await screen.findByText("$3.00")).toBeTruthy();
    expect(screen.queryByText("0.00%")).toBeNull();
  });
  it("dismissed classes do not replace the Warning / Error event totals", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1.2345,
      openai_cost_usd: 0.5,
      anthropic_cost_usd: 0.25,
      warnings: 3,
      errors: 2,
      alert_classes_active: 0,
      alert_classes_dismissed: 5,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("3 / 2")).toBeTruthy();
    expect(screen.queryByText("5 events")).toBeNull();
    expect(screen.queryByText("All clear")).toBeNull();
  });
  it("keeps Warning / Error counts compact without opening an inline class list", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 1, output: 1, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      openai_cost_usd: 0,
      anthropic_cost_usd: 0,
      warnings: 10_000,
      errors: 0,
      alert_classes_active: 1,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("10000 / 0")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Warnings \/ errors/ })).toBeNull();
    expect(screen.queryByText("disk_pressure")).toBeNull();
  });
  it("error without data shows retry and clicking it refetches", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsError = new Error("stats endpoint 500");
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect((await screen.findAllByText("!")).length).toBeGreaterThanOrEqual(7);
    expect(document.querySelectorAll(".text-destructive").length).toBeGreaterThan(0);
    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(retry.getAttribute("title")).toBe("stats endpoint 500");
    const reads = transport.getStatsDashboard.mock.calls.length;
    fireEvent.click(retry);
    expect(transport.getStatsDashboard.mock.calls.length).toBe(reads + 1);
  });
  it("a failing background read keeps the last numbers and labels them with the warning", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 1,
      openai_cost_usd: 0.5,
      anthropic_cost_usd: 0.25,
      warnings: 2,
      errors: 1,
      alert_classes_active: 1,
      alert_classes_dismissed: 2,
      plugin_stats: [],
    };
    state.statsError = new Error("stats endpoint 500");
    vi.useFakeTimers();
    try {
      wrap(<AgentSidebar {...handlers} />);
      openStatus();
      await vi.advanceTimersByTimeAsync(0);
      expect(screen.getAllByText("5").length).toBeGreaterThan(0);
      // meiva:note
      // The panel's own poll re-reads the dashboard; that read fails. The numbers stay and
      // the failure is labelled, instead of the panel blanking once it had them.
      await vi.advanceTimersByTimeAsync(30_000);
      // meiva:note
      // The read retries once (retry: 1); the retry's own timer lands at the end of the window.
      await vi.advanceTimersByTimeAsync(1_000);
      // meiva:note
      // The icon is the panel's own failure marker; a card title carrying the same text would
      // not prove the panel noticed the failure.
      expect(screen.getByRole("img", { name: "stats endpoint 500" })).toBeTruthy();
      expect(screen.getAllByText("5").length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });
  it("tokens compact: < 1000 raw, < 1M as k, >= 1M as M", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 0,
      window_hours: 24,
      tokens: { input: 1_500_000, output: 0, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      openai_cost_usd: 0,
      anthropic_cost_usd: 0,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("1.5M")).toBeTruthy();
  });
  it("null provider cost slices (unavailable model catalog) show placeholder instead of $0.00", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.stats = {
      live_count: 2,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 1.5,
      openai_cost_usd: null,
      anthropic_cost_usd: null,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("$1.50")).toBeTruthy();
    expect(await screen.findAllByText("—")).toHaveLength(2);
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});

describe("plugin status cards", () => {
  function statsWith(pluginStats: StatsDashboard["plugin_stats"]): StatsDashboard {
    return {
      live_count: 7,
      window_hours: 24,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      openai_cost_usd: 0,
      anthropic_cost_usd: 0,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: pluginStats,
    };
  }
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
  it("keeps each reset window and supporting detail in visible text", async () => {
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
    openStatus();
    expect(await screen.findByText("Codex · zhiyuan0206")).toBeTruthy();
    expect((await screen.findByText(/5h reset 09-29 02:00 UTC\+08:00/)).textContent).toBe(
      "5h reset 09-29 02:00 UTC+08:00\nWeekly reset 10-04 17:14 UTC+08:00",
    );
    expect(
      await screen.findByText("5h 52% remaining · Weekly 68% remaining · 2 resets available"),
    ).toBeTruthy();
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
  it("hides stale windowed values while retaining the live-agent count and plugin cards", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    state.pluginStatDeclarations = [{ plugin: "deepseek_balance", id: "bal", label: "DeepSeek" }];
    state.stats = {
      live_count: 5,
      window_hours: 24,
      tokens: { input: 12_345, output: 6_789, cache_read: 0, cache_hit_pct: 80 },
      cost_usd: 10.5,
      openai_cost_usd: 4.2,
      anthropic_cost_usd: 3.1,
      warnings: 2,
      errors: 1,
      alert_classes_active: 1,
      alert_classes_dismissed: 2,
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
    };
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    expect(await screen.findByText("5")).toBeTruthy();
    expect(await screen.findByText("$42.80")).toBeTruthy();
    expect(await screen.findAllByText("…")).toHaveLength(6);
    expect(screen.queryByText("19.1k")).toBeNull();
    expect(screen.queryByText("80.00%")).toBeNull();
    expect(screen.queryByText("$10.50")).toBeNull();
    expect(screen.queryByText("$4.20")).toBeNull();
    expect(screen.queryByText("$3.10")).toBeNull();
    expect(await screen.findAllByTitle("Updating for 1h…")).toHaveLength(6);
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
    state.stats = {
      live_count: 5,
      window_hours: 168,
      applied_window_hours: 84,
      tokens: { input: 100, output: 50, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 1,
      openai_cost_usd: 0.5,
      anthropic_cost_usd: 0.2,
      warnings: 0,
      errors: 0,
      alert_classes_active: 0,
      alert_classes_dismissed: 0,
      plugin_stats: [],
    };
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
  it("separates Live agents above the Usage section and renders each stat label", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    state.statsWindowHours = 1;
    wrap(<AgentSidebar {...handlers} />);
    openStatus();
    const live = await screen.findByText("Live agents");
    const usage = await screen.findByText("Usage");
    expect(live.compareDocumentPosition(usage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await screen.findByText("Warnings / errors")).toBeTruthy();
    expect(await screen.findByText("Tokens")).toBeTruthy();
    expect(await screen.findByText("Cache hit")).toBeTruthy();
    expect(await screen.findByText("Cost")).toBeTruthy();
    expect(await screen.findByText("OpenAI API")).toBeTruthy();
    expect(await screen.findByText("Anthropic API")).toBeTruthy();
    expect(screen.queryByText("Average turn time")).toBeNull();
  });
});
