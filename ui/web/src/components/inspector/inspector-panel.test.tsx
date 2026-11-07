import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentInspectLive,
  AgentInspectStatistics,
  InspectWidget,
} from "@/lib/contracts/types";
import { AGENTS_QUERY_KEY } from "@/lib/fold/agents";
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
    expect(screen.queryByText("Configuration overlay")).toBeNull();
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
    // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
    const livenessSection = screen.getByText("Liveness").closest("section");
    expect(livenessSection).not.toBeNull();
    expect(within(livenessSection!).getByText("Harness")).toBeTruthy();
    expect(within(livenessSection!).getByText("Model")).toBeTruthy();
    expect(within(livenessSection!).getByText("Claude Opus 4.8")).toBeTruthy();
    const overlaySection = screen.getByText("Configuration overlay").closest("section");
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
    expect(screen.getByText("Idle")).toBeTruthy();
    expect(screen.getByText("30m")).toBeTruthy();

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

  it("renders in-place metric breathing bars for persistent sections without speculative sparse sections while pending", () => {
    const qc = createInspectorQueryClient();
    qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture({ harness: "meiva" }));
    getAgentInspectLive.mockReturnValue(new Promise<AgentInspectLive>(() => undefined));
    getAgentInspectStatistics.mockReturnValue(new Promise<AgentInspectStatistics>(() => undefined));
    getAgentInspectWidgets.mockReturnValue(new Promise<InspectWidget[]>(() => undefined));

    render(<InspectorPanel agentId={1} />, qc);

    expect(screen.getByRole("link", { name: "Open run timeline" })).toBeTruthy();

    const liveness = screen.getByLabelText("Liveness loading");
    expect(liveness.getAttribute("aria-busy")).toBe("true");
    for (const label of ["Birth", "Next heartbeat", "Last pause", "Harness", "Model", "Machine", "Status"]) {
      expect(within(liveness).getByText(label)).toBeTruthy();
    }
    expect(liveness.querySelectorAll(".animate-pulse")).toHaveLength(7);

    const activity = screen.getByLabelText("Activity loading");
    expect(activity.getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByText("Activity")).toHaveLength(1);
    for (const label of ["Cost", "LLM calls", "Tokens", "Cache hit", "TPS", "LLM output", "Code execution", "Idle"]) {
      expect(within(activity).getByText(label)).toBeTruthy();
    }
    expect(activity.querySelectorAll(".animate-pulse")).toHaveLength(8);

    expect(screen.queryByText("Page")).toBeNull();
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration overlay")).toBeNull();
    expect(screen.queryByText("Extensions")).toBeNull();
    expect(screen.queryByText("Context")).toBeNull();
    expect(screen.queryByText("Notice")).toBeNull();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("keeps Liveness and Activity section and card DOM nodes mounted in place across initial resolve and window switches", async () => {
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
    const birthCard = within(livenessSection).getByText("Birth").parentElement;
    const activitySection = screen.getByLabelText("Activity loading");
    const costCard = within(activitySection).getByText("Cost").parentElement;
    expect(birthCard).not.toBeNull();
    expect(costCard).not.toBeNull();

    releaseLive(liveFixture({ shells: [], config_overlay: {} }));
    releaseWindowed(fixture({ window_hours: 24 }));

    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("test-host")).toBeTruthy());

    expect(screen.getByText("Liveness").closest("section")).toBe(livenessSection);
    expect(within(livenessSection).getByText("Birth").parentElement).toBe(birthCard);
    expect(screen.getByText("Activity").closest("section")).toBe(activitySection);
    expect(within(activitySection).getByText("Cost").parentElement).toBe(costCard);

    fireEvent.change(screen.getByLabelText("Cost + activity window"), {
      target: { value: "1" },
    });
    await waitFor(() => expect(screen.getByLabelText("Activity loading")).toBe(activitySection));
    expect(within(activitySection).getByText("Cost").parentElement).toBe(costCard);
  });

  it("shows current state while statistics and plugins are pending", async () => {
    getAgentInspectWidgets.mockReturnValue(new Promise<InspectWidget[]>(() => undefined));
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

  it("a current-state failure does not hide statistics or extension state", async () => {
    getAgentInspectLive.mockRejectedValue(new Error("runner unreachable"));
    getAgentInspectWidgets.mockRejectedValue(new Error("plugin unavailable"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("$0.4213")).toBeTruthy());
    expect(screen.getByText("runner unreachable")).toBeTruthy();
    expect(screen.getByText("Inspector extensions unavailable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry extensions" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry extensions" }));
    await waitFor(() => expect(getAgentInspectWidgets).toHaveBeenCalledTimes(2));
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
    expect(getAgentInspectWidgets).toHaveBeenCalledTimes(1);
  });

  it("replaces the prior window with skeletons while a new window is pending", async () => {
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
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
    expect(screen.getByText("dev-server")).toBeTruthy();
  });

  it("unmetered cache renders N/A without hiding cost or token usage", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture({
      cost: {
        cost_usd: 3, unpriced_calls: 0, llm_calls: 2,
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
          unpriced_calls: 0,
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
          unpriced_calls: 0,
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
});
