import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { foldNotices } from "@/lib/fold/notices";
import { inspectLiveQueryKey } from "@/lib/inspector/inspector-queries";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import type { SystemEvent } from "@/lib/contracts/types";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  liveFixture,
  panelState,
  QueryClientProvider,
  render,
  rosterFixture,
  setupInspectorTest,
  streamHandlers,
  widgetFixture,
} from "./inspector-test-mock";




import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel", () => {
  it("never renders an inline Notice reply section inside the inspector body", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 1,
          title: "Approve deploy?",
          content: "Can we push to prod?",
          priority: "P0",
          require_response: true,
          blocking: true,
          created_at: "2026-06-15T13:30:00Z",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
    expect(screen.queryByText("Notice")).toBeNull();
    expect(screen.queryByText("Approve deploy?")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("updates the notice without rereading statistics when notice SSE arrives", async () => {
    const queryClient = createInspectorQueryClient();
    renderWithBrowser(
      <QueryClientProvider client={queryClient}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const liveReads = getAgentInspectLive.mock.calls.length;

    act(() => {
      const event = { agent_id: 1, role: "notice_posted", notice_id: 1, priority: "P2", title: "t", task_id: null } as SystemEvent;
      for (const { key } of foldNotices(event).invalidations) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      streamHandlers.system?.(event);
    });

    // meiva:note
    // Two observables, both read from the cache the panel writes to: the notice's own query is
    // invalidated, and the windowed statistics query is not — an invalidation is an action on the
    // cache, and a refetch follows from it, so the flag is what a wrong call would leave behind.
    // Reading it beats replacing `invalidateQueries` with a spy.
    const live = queryClient.getQueryCache().find({ queryKey: inspectLiveQueryKey(1) });
    expect(live?.state.isInvalidated).toBe(true);
    const windowed = queryClient.getQueryCache().findAll({ queryKey: ["agent-inspect"] });
    expect(windowed.length).toBeGreaterThan(0);
    expect(windowed.some((query) => query.state.isInvalidated)).toBe(false);

    await waitFor(() => expect(getAgentInspectLive.mock.calls.length).toBeGreaterThan(liveReads));
    expect(getAgentInspectStatistics).toHaveBeenCalledTimes(1);
  });

  it("hides available-but-empty shells and an empty config overlay", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], config_overlay: {} }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration")).toBeNull();
  });

  it("does not render observation details", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
    expect(screen.queryByText("Observation")).toBeNull();
    expect(screen.queryByText(/Machine probe:/)).toBeNull();
  });

  it("does not report an unavailable shell observation as an empty set", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], shells_available: false }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Shell observation unavailable")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
  });

  it("renders skill-list config keys in canonical dash spelling (display_name)", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        config_overlay: {
          skills_to_inject_into_system_prompt: ["example_skill", "*"],
          llm_model: "claude-opus-4-8",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Configuration")).toBeTruthy());
    expect(screen.getByText('["example-skill","*"]')).toBeTruthy();
    expect(screen.queryByText(/example_skill/)).toBeNull();
    expect(screen.getByText("Claude Opus 4.8")).toBeTruthy();
  });

  it("window selector re-queries with the chosen hours", async () => {
    getAgentInspectStatistics.mockResolvedValueOnce(fixture({ window_hours: 24 }));
    getAgentInspectStatistics.mockResolvedValue(fixture({ window_hours: 1 }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspectStatistics).toHaveBeenCalledWith(1, 24, expect.any(AbortSignal));
    expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("24");

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "1" } });

    await waitFor(() =>
      expect(getAgentInspectStatistics).toHaveBeenCalledWith(1, 1, expect.any(AbortSignal)),
    );
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("1"),
    );
  });

  it("labels a retention-clamped selected window with the applied hours", async () => {
    panelState.hours = 168;
    getAgentInspectStatistics.mockResolvedValue(
      fixture({ window_hours: 168, applied_window_hours: 84 }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const select = screen.getByLabelText<HTMLSelectElement>("Cost + activity window");
    expect(select.selectedOptions[0].text).toBe("7d · 84h");
    expect(Array.from(select.options).find((option) => option.value === "24")?.text).toBe(
      "24h",
    );
  });

  it("offers only cumulative and time-based windows", async () => {
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Persistent shells");
    const select = screen.getByLabelText<HTMLSelectElement>("Cost + activity window");
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "all", "0", "1", "24", "168",
    ]);
  });

  it("shows an error message when the fetch fails", async () => {
    getAgentInspectLive.mockRejectedValue(new Error("boom"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("stale refresh: a failed background poll keeps the sections quietly (no stale dot, no cold-error swap)", async () => {
    const qc = createInspectorQueryClient();
    getAgentInspectStatistics.mockResolvedValueOnce(fixture());
    renderWithBrowser(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    getAgentInspectStatistics.mockRejectedValue(new Error("refresh failed"));
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["agent-inspect", 1, 24] });
    });

    expect(screen.queryByLabelText("Live refresh failing")).toBeNull();
    expect(screen.getByText("Persistent shells")).toBeTruthy();
    expect(screen.queryByText("refresh failed")).toBeNull();
  });

  it("refuses an inspect payload whose agent_id does not match the selection", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ agent_id: Number.NaN }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalled());
    expect(screen.queryByText("dev-server")).toBeNull();
    await waitFor(() => expect(screen.getByText("No data")).toBeTruthy());
  });

  it("renders shell rows as plain text when a shell id is invalid", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        shells: [
          { id: Number.NaN, name: "bad-shell", created_at: null, uptime_seconds: 10 },
        ],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("bad-shell")).toBeTruthy());
    expect(screen.getByText("bad-shell").closest("a")).toBeNull();
  });
});


describe("InspectorPanel plugin widgets", () => {
  it("renders a taskList widget as a titled section of fleet-task links", async () => {
    getAgentInspectWidgets.mockResolvedValue([widgetFixture()]);
    render(<InspectorPanel agentId={1} />);

    expect(await screen.findByText("Tasks")).toBeTruthy();
    const first = await screen.findByRole("link", { name: /Ship the inspector fix/ });
    expect(first.getAttribute("href")).toBe("/fleet?task=42");
    expect(screen.getByRole("link", { name: /Reply to QA/ }).getAttribute("href")).toBe(
      "/fleet?task=43",
    );
  });

  it("puts the fleet task list above the runtime sections", async () => {
    getAgentInspectWidgets.mockResolvedValue([widgetFixture()]);
    render(<InspectorPanel agentId={1} />);

    const tasks = await screen.findByText("Tasks");
    const shells = await screen.findByText("Persistent shells");
    const before = (first: Node, second: Node) =>
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(before(tasks, shells)).toBeTruthy();
  });

  it("slots a widget into the section order (750: between context and activity)", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    getAgentInspectLive.mockResolvedValue(liveFixture());
    getAgentInspectWidgets.mockResolvedValue([widgetFixture({ order: 750 })]);
    render(<InspectorPanel agentId={1} />);

    const runLink = await screen.findByRole("link", { name: "Open run timeline" });
    const widgetLink = await screen.findByRole("link", { name: /Ship the inspector fix/ });
    const context = await screen.findByText("Context");
    const configOverlay = await screen.findByText("Configuration");
    const activity = screen.getByText("Activity");
    expect(
      runLink.compareDocumentPosition(widgetLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      configOverlay.compareDocumentPosition(widgetLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      context.compareDocumentPosition(widgetLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      widgetLink.compareDocumentPosition(activity) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    act(() => {
      useTimelineStore.getState().applyTokenUsage(0, 0, 0, 0, 0);
    });
  });

  it("renders no widget area when no plugin registers one", async () => {
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(getAgentInspectWidgets).toHaveBeenCalled());
    expect(screen.queryByText("Tasks")).toBeNull();
  });

  it("a widgets fetch failure leaves the rest of the panel intact", async () => {
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    getAgentInspectWidgets.mockRejectedValue(new Error("HTTP 500: widgets unavailable"));
    render(<InspectorPanel agentId={1} />);

    await screen.findByRole("link", { name: "Open run timeline" });
    await screen.findAllByText("Activity");
    expect(screen.queryByText("Tasks")).toBeNull();
  });
});
