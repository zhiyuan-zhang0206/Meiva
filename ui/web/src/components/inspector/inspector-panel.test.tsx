import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentInspectLive,
  AgentInspectStatistics,
} from "@/lib/contracts/types";
import { AGENTS_QUERY_KEY } from "@/lib/fold/agents";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentRoster,
  liveFixture,
  QueryClientProvider,
  render,
  renderWithGlobalRetries,
  rosterFixture,
  setupInspectorTest,
} from "./inspector-test-mock";




import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel", () => {
  it("owns manual retry instead of inheriting the global automatic retry policy", async () => {
    getAgentInspectLive.mockRejectedValue(
      new Error("HTTP 503: inspector history query timed out; retry"),
    );
    renderWithGlobalRetries(<InspectorPanel agentId={1} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry inspector" })).toBeTruthy(),
    );
    expect(getAgentInspectLive).toHaveBeenCalledTimes(1);
  });

  it("shows a failed cold load with an explicit retry action", async () => {
    getAgentInspectLive
      .mockRejectedValueOnce(new Error("HTTP 503: inspector history query timed out; retry"))
      .mockResolvedValueOnce(liveFixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() =>
      expect(screen.getByText("HTTP 503: inspector history query timed out; retry")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry inspector" }));
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspectLive).toHaveBeenCalledTimes(2);
  });

  it("never renders the previous agent while the new agent inspect is pending", async () => {
    let resolveAgentB: ((value: AgentInspectLive) => void) | undefined;
    getAgentInspectLive.mockImplementation((agentId) => {
      if (agentId === 1) {
        return Promise.resolve(
          liveFixture({
            agent_id: 1,
            machine: "agent-a-host",
            shells: [
              {
                id: 91,
                name: "agent-a-private-shell",
                created_at: null,
                uptime_seconds: 10,
              },
            ],
            config_overlay: {
              custom_flag: "agent-a-secret-flag",
            },
          }),
        );
      }
      return new Promise<AgentInspectLive>((resolve) => {
        resolveAgentB = resolve;
      });
    });
    const qc = createInspectorQueryClient();
    const view = renderWithBrowser(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("agent-a-private-shell")).toBeTruthy());
    expect(screen.getByText("agent-a-host")).toBeTruthy();
    expect(screen.getByText("agent-a-secret-flag")).toBeTruthy();

    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(getAgentInspectLive).toHaveBeenCalledWith(2, expect.any(AbortSignal)),
    );

    expect(screen.queryByText("agent-a-private-shell")).toBeNull();
    expect(screen.queryByText("agent-a-host")).toBeNull();
    expect(screen.queryByText("agent-a-secret-flag")).toBeNull();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();

    resolveAgentB?.(liveFixture({ agent_id: 2, machine: "agent-b-host", shells: [], config_overlay: {} }));
    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
    expect(screen.getByText("agent-b-host")).toBeTruthy();
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration")).toBeNull();
  });

  it("renders sections from their current-state and statistics responses", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-14T12:00:00Z"));
    getAgentInspectStatistics.mockResolvedValue(fixture());
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("dev-server")).toBeTruthy();
    expect(screen.getByText("(unnamed)")).toBeTruthy();
    expect(screen.getByText("watcher")).toBeTruthy();
    expect(screen.getByText("2h 14m")).toBeTruthy();
    expect(screen.getByText("45s")).toBeTruthy();
    expect(screen.getByText("11m")).toBeTruthy();
    expect(screen.queryByText(/^\[\d{4}-\d{2}-\d{2} /)).toBeNull();
    expect(screen.getByText("dev-server").closest("a")?.getAttribute("href")).toBe("/shell/1/0");
    expect(screen.getByText("watcher").closest("a")?.getAttribute("href")).toBe("/shell/1/2");

    // meiva:note
    // The model is the agent's state, not an override: it reads in Liveness beside the
    // harness, and the overlay no longer repeats it as llm_model (user 2026-10-21).
    // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
    const livenessSection = screen.getByText("Liveness").closest("section");
    expect(livenessSection).not.toBeNull();
    expect(within(livenessSection!).getByText("Harness")).toBeTruthy();
    expect(within(livenessSection!).getByText("Model")).toBeTruthy();
    expect(within(livenessSection!).getByText("Claude Opus 4.8")).toBeTruthy();
    const overlaySection = screen.getByText("Configuration").closest("section");
    expect(overlaySection).not.toBeNull();
    expect(within(overlaySection!).queryByText("llm_model")).toBeNull();
    expect(within(overlaySection!).getByText("0.7")).toBeTruthy();

    expect(screen.getByText("$0.4213")).toBeTruthy();
    expect(screen.getByText("1.20M / 84.0k")).toBeTruthy();
    expect(screen.getByText("142")).toBeTruthy();
    expect(screen.getByText("91.70%")).toBeTruthy();

    const activitySection = screen.getByText("Activity").closest("section");
    expect(activitySection).not.toBeNull();
    expect(within(activitySection!).getByText("TPS")).toBeTruthy();
    expect(within(activitySection!).getByText("42.5")).toBeTruthy();
    expect(screen.queryByText("Active rate")).toBeNull();
    expect(screen.queryByText("50%")).toBeNull();
    expect(screen.queryByText("LLM stage")).toBeNull();
    expect(screen.getByText("20m")).toBeTruthy();
    expect(screen.getByText("8m")).toBeTruthy();
    expect(screen.queryByText("Idle")).toBeNull();

    expect(screen.getByText("Liveness")).toBeTruthy();
    expect(screen.queryByText("State")).toBeNull();
    expect(screen.queryByText("every 5m")).toBeNull();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("links the active agent to its run timeline", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const link = screen.getByRole("link", { name: "Open run timeline" });
    expect(link.getAttribute("href")).toBe("/insights/run/1");
    const classes = link.className.split(" ");
    expect(classes).toContain("rounded");
    expect(classes).toContain("bg-sidebar-accent/40");
  });

  it("renders no Alerts section (user ruling 2026-08-29)", async () => {
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.queryByText("Alerts")).toBeNull();
  });

  it("renders one spinner per pending section with no metric card skeleton or speculative sparse sections", () => {
    const qc = createInspectorQueryClient();
    qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture({ harness: "meiva" }));
    getAgentInspectLive.mockReturnValue(new Promise<AgentInspectLive>(() => undefined));
    getAgentInspectStatistics.mockReturnValue(new Promise<AgentInspectStatistics>(() => undefined));

    render(<InspectorPanel agentId={1} />, qc);

    expect(screen.getByRole("link", { name: "Open run timeline" })).toBeTruthy();

    const liveness = screen.getByLabelText("Liveness loading");
    expect(liveness.getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByText("Liveness")).toBeNull();
    for (const label of ["Birth", "Next heartbeat", "Last pause", "Harness", "Model", "Machine", "Status"]) {
      expect(within(liveness).queryByText(label)).toBeNull();
    }
    expect(liveness.querySelectorAll(".animate-spin")).toHaveLength(1);
    expect(liveness.querySelectorAll(".animate-pulse")).toHaveLength(0);

    const activity = screen.getByLabelText("Activity loading");
    expect(activity.getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByText("Activity")).toBeNull();
    for (const label of ["Cost (subscription)", "Cost (API)", "LLM calls", "Tokens", "Cache hit", "TPS", "LLM output", "Code execution"]) {
      expect(within(activity).queryByText(label)).toBeNull();
    }
    expect(activity.querySelectorAll(".animate-spin")).toHaveLength(0);
    expect(activity.querySelectorAll(".animate-pulse")).toHaveLength(0);

    expect(screen.queryByText("Page")).toBeNull();
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration")).toBeNull();
    expect(screen.queryByText("Context")).toBeNull();
    expect(screen.queryByText("Notice")).toBeNull();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("keeps Liveness and Activity section DOM nodes mounted in place across initial resolve and window switches without metric skeleton cards", async () => {
    let releaseLive: (v: AgentInspectLive) => void = () => undefined;
    let releaseWindowed: (v: AgentInspectStatistics) => void = () => undefined;
    getAgentInspectLive.mockReturnValue(
      new Promise<AgentInspectLive>((resolve) => {
        releaseLive = resolve;
      }),
    );
    getAgentInspectStatistics.mockImplementation((_agentId, hours) => {
      if (hours === 24) {
        return new Promise<AgentInspectStatistics>((resolve) => {
          releaseWindowed = resolve;
        });
      }
      return new Promise<AgentInspectStatistics>(() => undefined);
    });

    render(<InspectorPanel agentId={1} />);

    const livenessSection = screen.getByLabelText("Liveness loading");
    expect(within(livenessSection).queryByText("Birth")).toBeNull();
    const activitySection = screen.getByLabelText("Activity loading");
    expect(within(activitySection).queryByText("Cost (API)")).toBeNull();

    releaseLive(liveFixture({ shells: [], config_overlay: {} }));
    releaseWindowed(fixture({ window_hours: 24 }));

    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("test-host")).toBeTruthy());

    expect(screen.getByText("Liveness").closest("section")).toBe(livenessSection);
    expect(within(livenessSection).getByText("Birth")).toBeTruthy();
    expect(screen.getByText("Activity").closest("section")).toBe(activitySection);
    expect(within(activitySection).getByText("Cost (API)")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Cost + activity window"), {
      target: { value: "1" },
    });
    await waitFor(() => expect(screen.getByLabelText("Activity loading")).toBe(activitySection));
    expect(within(activitySection).queryByText("Cost (API)")).toBeNull();
  });

  it("shows current state while statistics are pending", async () => {
    let releaseWindowed: (v: AgentInspectStatistics) => void = () => undefined;
    getAgentInspectStatistics.mockReturnValue(
      new Promise<AgentInspectStatistics>((resolve) => {
        releaseWindowed = resolve;
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("dev-server")).toBeTruthy());
    expect(screen.queryByLabelText("Liveness loading")).toBeNull();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();

    releaseWindowed(fixture({ window_hours: 24 }));
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    expect(screen.queryByLabelText("Activity loading")).toBeNull();
    expect(screen.queryByLabelText("Liveness loading")).toBeNull();
  });

  it("a current-state failure leaves statistics available", async () => {
    getAgentInspectLive.mockRejectedValue(new Error("runner unreachable"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    expect(screen.getByText("runner unreachable")).toBeTruthy();
    expect(getAgentInspectLive).toHaveBeenCalledTimes(1);
    expect(getAgentInspectStatistics).toHaveBeenCalledTimes(1);
  });

  it("statistics failure leaves current state available and retries only statistics", async () => {
    getAgentInspectStatistics.mockRejectedValueOnce(new Error("history unavailable"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("dev-server")).toBeTruthy());
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry windowed data" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Retry windowed data" }));
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    expect(getAgentInspectLive).toHaveBeenCalledTimes(1);
  });

  it("replaces the prior window's values with spinners while a new window is pending", async () => {
    getAgentInspectStatistics.mockImplementation((_agentId, hours) => {
      if (hours === 24) return Promise.resolve(fixture({ window_hours: 24 }));
      return new Promise<AgentInspectStatistics>(() => undefined);
    });
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Cost + activity window"), {
      target: { value: "1" },
    });

    await waitFor(() =>
      expect(getAgentInspectStatistics).toHaveBeenCalledWith(1, 1, expect.any(AbortSignal)),
    );
    expect(screen.queryByText("$0.4213")).toBeNull();
    const activity = screen.getByLabelText("Activity loading");
    expect(activity.querySelectorAll(".animate-spin").length).toBeGreaterThan(0);
    expect(activity.querySelectorAll(".animate-pulse")).toHaveLength(0);
    expect(screen.getByText("dev-server")).toBeTruthy();
  });

  it("unmetered cache renders N/A without hiding cost or token usage", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture({
      cost: {
        cost_usd: 3, cost_usd_api: 3, cost_usd_subscription: 0,
        unpriced_calls: 0, unpriced_calls_api: 0, unpriced_calls_subscription: 0, incomplete_usage_reports: 0, llm_calls: 2,
        tokens_in: 6000, tokens_out: 0, tokens_cached: 0,
        tokens_reasoning: 0, cache_hit_pct: null,
      },
    }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("N/A")).toBeTruthy());
    expect(screen.getByText("$3.0000")).toBeTruthy();
    expect(screen.queryByText("0.00%")).toBeNull();
  });

  it("formats token counts with B/T tiers (task #824): 2176.67M → 2.18B", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.4213,
          cost_usd_api: 0.4213,
          cost_usd_subscription: 0,
          unpriced_calls: 0,
          unpriced_calls_api: 0,
          unpriced_calls_subscription: 0,
          incomplete_usage_reports: 0,
          llm_calls: 142,
          tokens_in: 2_176_670_000,
          tokens_out: 3_420_000,
          tokens_cached: 0,
          tokens_reasoning: 0,
          cache_hit_pct: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("2.18B / 3.42M")).toBeTruthy();
  });

  it("formats token counts with the T tier past 1000B (task #824)", async () => {
    getAgentInspectStatistics.mockResolvedValue(
      fixture({
        cost: {
          cost_usd: 0.0,
          cost_usd_api: 0.0,
          cost_usd_subscription: 0.0,
          unpriced_calls: 0,
          unpriced_calls_api: 0,
          unpriced_calls_subscription: 0,
          incomplete_usage_reports: 0,
          llm_calls: 0,
          tokens_in: 1_500_000_000_000,
          tokens_out: 999,
          tokens_cached: 0,
          tokens_reasoning: 0,
          cache_hit_pct: 0,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.getByText("1.50T / 999")).toBeTruthy();
  });

  it("renders Open run timeline for native meiva harness", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Open run timeline" })).toBeTruthy(),
    );
    expect(screen.getByRole("link", { name: "Open run timeline" }).getAttribute("href")).toBe(
      "/insights/run/1",
    );
  });

  it.each(["claude_code", "codex", "antigravity"] as const)(
    "hides Open run timeline for external harness %s",
    async (harness) => {
      getAgentRoster.mockResolvedValue(rosterFixture({ harness }));
      render(<InspectorPanel agentId={1} />);
      await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
      expect(screen.queryByRole("link", { name: "Open run timeline" })).toBeNull();
    },
  );

  it("hides Open run timeline while harness is unresolved", async () => {
    getAgentRoster.mockReturnValue(new Promise(() => undefined));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.queryByRole("link", { name: "Open run timeline" })).toBeNull();
  });

  it("hides Schedules section when the agent has no associated schedules", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ schedules: [] }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.queryByText("Schedules")).toBeNull();
  });

  it("renders owned and cluster-level schedules with scope badge, status, and deep links", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-14T12:00:00Z"));
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        schedules: [
          {
            id: 11,
            name: "cluster-watchdog",
            enabled: true,
            status: "running",
            trigger_kind: "cron",
            cron_expr: "*/5 * * * *",
            interval_seconds: null,
            run_at: null,
            next_launch_at: "2026-06-14T12:05:00Z",
            owner_agent_id: null,
            created_by_agent_id: 1,
            machine: "gateway",
          },
          {
            id: 12,
            name: "broken-sync",
            enabled: true,
            status: "error",
            trigger_kind: "every",
            cron_expr: null,
            interval_seconds: 300,
            run_at: null,
            next_launch_at: null,
            owner_agent_id: 1,
            created_by_agent_id: 1,
            machine: "test-host",
          },
          {
            id: 10,
            name: "daily-digest",
            enabled: true,
            status: "scheduled",
            trigger_kind: "every",
            cron_expr: null,
            interval_seconds: 3600,
            run_at: null,
            next_launch_at: "2026-06-14T13:00:00Z",
            owner_agent_id: 1,
            created_by_agent_id: 1,
            machine: "test-host",
          },
          {
            id: 13,
            name: "paused-job",
            enabled: false,
            status: "stopped",
            trigger_kind: "at",
            cron_expr: null,
            interval_seconds: null,
            run_at: "2026-06-15T00:00:00Z",
            next_launch_at: null,
            owner_agent_id: 1,
            created_by_agent_id: 1,
            machine: "test-host",
          },
        ],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Schedules")).toBeTruthy());
    const section = screen.getByText("Schedules").closest("section");
    expect(section).not.toBeNull();
    expect(within(section!).getByText("4")).toBeTruthy();

    const clusterLink = within(section!).getByText("cluster-watchdog").closest("a");
    expect(clusterLink?.getAttribute("href")).toBe("/control/schedules?schedule=11");
    expect(within(clusterLink!).getByTestId("schedule-scope-cluster").textContent).toBe("Cluster");
    expect(within(clusterLink!).getByText("Running")).toBeTruthy();
    expect(clusterLink?.getAttribute("title")).toContain("cron */5 * * * *");
    expect(clusterLink?.getAttribute("title")).toContain("gateway");

    const errorLink = within(section!).getByText("broken-sync").closest("a");
    expect(errorLink?.getAttribute("href")).toBe("/control/schedules?schedule=12");
    expect(within(errorLink!).queryByTestId("schedule-scope-cluster")).toBeNull();
    expect(within(errorLink!).getByTestId("schedule-dot-error")).toBeTruthy();
    expect(within(errorLink!).getByText("Error")).toBeTruthy();

    const ownedLink = within(section!).getByText("daily-digest").closest("a");
    expect(ownedLink?.getAttribute("href")).toBe("/control/schedules?schedule=10");
    expect(within(ownedLink!).queryByTestId("schedule-scope-cluster")).toBeNull();
    expect(within(ownedLink!).getByTestId("schedule-dot-scheduled")).toBeTruthy();

    const stoppedLink = within(section!).getByText("paused-job").closest("a");
    expect(stoppedLink?.getAttribute("href")).toBe("/control/schedules?schedule=13");
    expect(within(stoppedLink!).getByTestId("schedule-dot-stopped")).toBeTruthy();
    expect(within(stoppedLink!).getByText("Stopped")).toBeTruthy();
  });
});
