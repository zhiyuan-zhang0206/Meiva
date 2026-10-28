import { useQuery } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { formatAbsolute, formatRelative, formatUpcoming } from "@/lib/format/time";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "@/lib/fold/agents";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgent,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentRoster,
  liveFixture,
  QueryClientProvider,
  render,
  rosterFixture,
  setupInspectorTest,
  useFrozenClock,
} from "./inspector-test-mock";




import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel liveness identity rows", () => {
  it("names the roster's harness and the model the agent was born on", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "codex" }));
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    expect(within(livenessSection!).getByText("Codex")).toBeTruthy();
    expect(within(livenessSection!).getByText("claude-opus-4-8")).toBeTruthy();
  });

  it("hides TPS, LM Output, and Code Execution in Activity for external harnesses", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "codex" }));
    getAgentInspectLive.mockResolvedValue(liveFixture());
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Cost")).toBeTruthy());
    const activitySection = screen.getByText("Activity").closest("section");
    expect(within(activitySection!).getByText("Cost")).toBeTruthy();
    expect(within(activitySection!).getByText("LLM calls")).toBeTruthy();
    expect(within(activitySection!).getByText("Idle")).toBeTruthy();
    expect(within(activitySection!).queryByText("TPS")).toBeNull();
    expect(within(activitySection!).queryByText("LM output")).toBeNull();
    expect(within(activitySection!).queryByText("Code execution")).toBeNull();
  });

  it("falls back to an em dash when the roster has not loaded", async () => {
    getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
    getAgentInspectLive.mockResolvedValue(liveFixture({ config_overlay: { auto_compact_fraction: 0.7 } }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    for (const label of ["Harness", "Model"]) {
      const cell = within(livenessSection!).getByText(label).closest("div");
      expect(within(cell!).getByText("—")).toBeTruthy();
    }
  });
});

describe("InspectorPanel heartbeat cells (merged into Liveness, Task #1195)", () => {
  it("shows the projected next check-in as a local clock time when idle and un-paused", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: "2026-10-17T16:30:00Z", paused_until: null, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("16:30")).toBeTruthy();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("gives a heartbeat that lands tomorrow a day label instead of a bare clock time", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    const nextAt = "2026-10-18T15:21:00Z";
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: nextAt, paused_until: null, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("Tomorrow 15:21")).toBeTruthy();
    expect(
      formatUpcoming(nextAt, new Date("2026-10-17T16:26:00Z"), { clockFormat: "24h" }),
    ).toBe("Tomorrow 15:21");
  });

  it("labels the active pause window and its remaining span when paused", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 900, next_at: null, paused_until: "2026-10-17T16:38:00Z", heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("paused until 16:38")).toBeTruthy());
    expect(screen.getByText("in 12m")).toBeTruthy();
  });

  it("keeps the pause as the answer for a running agent (user report 2026-10-28)", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        status: "running",
        heartbeat: { interval_s: 900, next_at: null, paused_until: "2026-10-18T15:21:00Z", heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    const nextCell = within(livenessSection!).getByText("Next heartbeat").closest("div");
    expect(within(nextCell!).getByText("paused until Tomorrow 15:21")).toBeTruthy();
    expect(within(nextCell!).getByText("in 22h 55m")).toBeTruthy();
  });

  it("counts the remaining pause span down as the panel clock ticks", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 900, next_at: null, paused_until: "2026-10-17T16:38:00Z", heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("in 12m")).toBeTruthy());

    // meiva:note
    // The sub line is computed from the panel's one-second clock, not once at open: a live query
    // that has not refreshed yet must still show a shorter span after the clock moves on, or the
    // panel would print "in 6h" for the rest of the pause.
    // meiva:link ui/web/src/docs/frontend-components/inspector/section-details/liveness.meiva.okf.md
    vi.setSystemTime(new Date("2026-10-17T16:32:00Z"));
    await waitFor(() => expect(screen.getByText("in 6m")).toBeTruthy(), { timeout: 3000 });
  });

  it("reads 'due' when a pause ran out between the last read and this tick", async () => {
    useFrozenClock("2026-10-17T16:26:00Z");
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 900, next_at: null, paused_until: "2026-10-17T16:20:00Z", heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    const nextCell = within(livenessSection!).getByText("Next heartbeat").closest("div");
    expect(within(nextCell!).getByText("due")).toBeTruthy();
  });

  it("shows 'pending' when a check-in is already queued but unprocessed", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: null, paused_until: null, heartbeat_pending: true, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.getByText("never paused")).toBeTruthy();
  });

  it("shows 'due' when the projected check-in is in the past", async () => {
    const nextAt = new Date(Date.now() - 240_000).toISOString();
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: { interval_s: 300, next_at: nextAt, paused_until: null, heartbeat_pending: false, last_pause: null },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("due")).toBeTruthy();
  });

  it("shows an em dash for a running agent with no active pause, plus the last pause from history", async () => {
    const at = new Date(Date.now() - 330_000).toISOString();
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        heartbeat: {
          interval_s: 300,
          next_at: null,
          paused_until: null,
          heartbeat_pending: false,
          last_pause: { at, duration_s: 1800 },
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const livenessSection = screen.getByText("Liveness").closest("section");
    expect(livenessSection).not.toBeNull();
    const nextCell = within(livenessSection!).getByText("Next heartbeat").closest("div");
    expect(nextCell).not.toBeNull();
    expect(within(nextCell!).getByText("—")).toBeTruthy();
    expect(screen.getByText("5m ago · 30m")).toBeTruthy();
  });
});

describe("InspectorPanel birth (cell in the merged Liveness section, Task #1195)", () => {
  it("birth cell shows relative + absolute, anchored to spawned_at", async () => {
    const spawned = "2026-06-14T12:00:00Z";
    getAgentInspectLive.mockResolvedValue(liveFixture({ spawned_at: spawned }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Birth")).toBeTruthy());
    expect(
      screen.getByText(`${formatRelative(spawned, new Date())}, ${formatAbsolute(spawned)}`),
    ).toBeTruthy();
  });
});


describe("InspectorPanel takeover status", () => {
  it("shows Impersonated for an active lease while the native lifecycle is idling", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture());
    getAgentInspectLive.mockResolvedValue(liveFixture({ status: "idling", liveness_state: "offline" }));
    const { container } = render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Impersonated")).toBeTruthy());
    expect(screen.queryByText("Idling")).toBeNull();
    expect(container.querySelector(".text-destructive")).not.toBeNull();
  });

  it("reacts to activation and release without retaining the takeover status", async () => {
    const qc = createInspectorQueryClient();
    qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture({ open_impersonation_status: "requested" }));
    getAgentInspectLive.mockResolvedValue(liveFixture({ status: "idling" }));
    render(<InspectorPanel agentId={1} />, qc);
    await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());

    act(() => { qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture()); });
    await waitFor(() => expect(screen.getByText("Impersonated")).toBeTruthy());
    act(() => {
      qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture({
        open_impersonation_session_id: null,
        open_impersonation_status: null,
      }));
    });
    await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());
    expect(screen.queryByText("Impersonated")).toBeNull();
    expect(getAgentRoster).not.toHaveBeenCalled();
    expect(getAgentInspectLive).toHaveBeenCalledTimes(1);
  });

  it.each(["requested", "accepted", null] as const)(
    "keeps the lifecycle status for a %s lease",
    async (phase) => {
      const qc = createInspectorQueryClient();
      getAgentRoster.mockResolvedValue(rosterFixture({ open_impersonation_status: phase }));
      getAgentInspectLive.mockResolvedValue(liveFixture({ status: "idling" }));
      render(<InspectorPanel agentId={1} />, qc);

      await waitFor(() => expect(qc.getQueryState(AGENTS_QUERY_KEY)?.status).toBe("success"));
      await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());
      expect(screen.queryByText("Impersonated")).toBeNull();
    },
  );

  it("keeps a terminated Inspector status even if an active roster card is still cached", async () => {
    const qc = createInspectorQueryClient();
    qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture());
    getAgentInspectLive.mockResolvedValue(liveFixture({ status: "terminated" }));
    render(<InspectorPanel agentId={1} />, qc);

    await waitFor(() => expect(screen.getByText("Terminated")).toBeTruthy());
    expect(screen.queryByText("Impersonated")).toBeNull();
  });

  it("keeps takeover status scoped to the selected agent across switches and roster removal", async () => {
    const qc = createInspectorQueryClient();
    qc.setQueryData(AGENTS_QUERY_KEY, rosterFixture());
    getAgentInspectLive.mockImplementation((id) => Promise.resolve(liveFixture({ agent_id: id, status: "idling" })));
    getAgentInspectStatistics.mockImplementation((id) => Promise.resolve(fixture({ agent_id: id })));
    const panel = (id: number) => (
      <QueryClientProvider client={qc}><InspectorPanel agentId={id} /></QueryClientProvider>
    );
    const view = renderWithBrowser(panel(2));
    await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());
    expect(screen.queryByText("Impersonated")).toBeNull();

    view.rerender(panel(1));
    await waitFor(() => expect(screen.getByText("Impersonated")).toBeTruthy());
    view.rerender(panel(2));
    await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());
    expect(screen.queryByText("Impersonated")).toBeNull();

    view.rerender(panel(1));
    await waitFor(() => expect(screen.getByText("Impersonated")).toBeTruthy());
    act(() => { qc.setQueryData(AGENTS_QUERY_KEY, { agents: [], ancestors: [] }); });
    await waitFor(() => expect(screen.getByText("Idling")).toBeTruthy());
    expect(screen.queryByText("Impersonated")).toBeNull();
  });
});

describe("InspectorPanel liveness (merged section, Task #1195)", () => {
  it("omits the redundant online state cell and the old last-judged cell", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ liveness_state: "online", last_probe_at: "2026-08-12T05:00:00Z" }),
    );
    const { container } = render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("online")).toBeNull();
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(screen.queryByText("Last judged")).toBeNull();
  });

  it("uses offline state without rendering a redundant state cell", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ liveness_state: "offline", last_probe_at: null }),
    );
    const { container } = render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("offline")).toBeNull();
    expect(container.querySelector(".text-destructive")).not.toBeNull();
    expect(screen.queryByText("never")).toBeNull();
  });

  it("shows the machine and lifecycle status (task #3904)", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ liveness_state: "online" }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.getByText("Machine")).toBeTruthy();
    expect(screen.getByText("test-host")).toBeTruthy();
    expect(screen.getByText("Status")).toBeTruthy();
    expect(screen.getByText("Running")).toBeTruthy();
  });

  it("orders the liveness cells birth → heartbeat → machine/status (task #3952)", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        liveness_state: "online",
        heartbeat: {
          interval_s: 300,
          next_at: "2026-09-18T06:00:00Z",
          paused_until: null,
          heartbeat_pending: false,
          last_pause: { at: "2026-09-18T05:00:00Z", duration_s: 1800 },
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    const section = screen.getByText("Liveness").closest("section");
    expect(section).not.toBeNull();
    const text = section!.textContent;
    const labels = ["Birth", "Next heartbeat", "Last pause", "Machine", "Status"];
    const idx = labels.map((label) => text.indexOf(label));
    expect(idx.every((i) => i >= 0)).toBe(true);
    expect(idx).toEqual([...idx].sort((a, b) => a - b));
  });
});

describe("InspectorPanel Context section", () => {
  beforeEach(() => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
  });
  afterEach(() => {
    act(() => { useTimelineStore.getState().applyTokenUsage(0, 0, 0, 0, 0); });
  });

  it("shows occupancy plus the soft and hard limits on one row", async () => {
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Context")).toBeTruthy());
    const used = screen.getByTestId("inspector-context-used");
    const limits = screen.getByTestId("inspector-context-limits");
    expect(used.textContent).toBe("109.6k / 1.00M");
    expect(limits.textContent).toBe("374.0k / 512.0k");
    expect(used.parentElement?.parentElement).toBe(limits.parentElement?.parentElement);
  });

  it("opens the shared breakdown as a floating panel under the gauge", async () => {
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    render(<InspectorPanel agentId={1} />);
    const trigger = await screen.findByTestId("context-meter-button");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger);
    const panel = await screen.findByTestId("context-breakdown-panel");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(panel.className).toContain("bottom-full");
    expect(panel.className).not.toContain("top-full");
  });

  it.each(["claude_code", "codex"] as const)("does not present the Meiva mirror as %s context", async (harness) => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness }));
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Activity");
    await waitFor(() => expect(getAgentRoster).toHaveBeenCalled());
    expect(screen.queryByText("Context")).toBeNull();
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
  });

  it("uses selected detail for a Meiva agent outside the roster without another read", async () => {
    getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
    const qc = createInspectorQueryClient();
    qc.setQueryData([...AGENT_DETAIL_QUERY_KEY, 1], rosterFixture({ harness: "meiva" }).agents[0]);
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    render(<InspectorPanel agentId={1} />, qc);
    expect(await screen.findByTestId("inspector-context-used")).toBeTruthy();
  });

  it("keeps the active detail owner's refresh query usable after subscribing", async () => {
    getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
    getAgent.mockResolvedValue(rosterFixture({ harness: "meiva" }).agents[0]);
    const qc = createInspectorQueryClient();
    const key = [...AGENT_DETAIL_QUERY_KEY, 1];
    function DetailOwner() {
      useQuery({ queryKey: key, queryFn: ({ signal }) => getAgent(1, signal) });
      return null;
    }
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    const view = renderWithBrowser(<QueryClientProvider client={qc}><DetailOwner /></QueryClientProvider>);
    await waitFor(() => expect(qc.getQueryState(key)?.status).toBe("success"));
    view.rerender(<QueryClientProvider client={qc}><DetailOwner /><InspectorPanel agentId={1} /></QueryClientProvider>);
    await screen.findByTestId("inspector-context-used");
    expect(getAgent).toHaveBeenCalledTimes(1);
    await act(async () => { await qc.invalidateQueries({ queryKey: key }); });
    expect(getAgent).toHaveBeenCalledTimes(2);
    expect(qc.getQueryState(key)?.status).toBe("success");
  });

  it("renders no Context section before the first LLM call", async () => {
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());
    expect(screen.queryByText("Context")).toBeNull();
  });
});
