import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS } from "@/lib/layout/layout";
import type * as TimeFormat from "@/lib/format/time";
import { BACK_VISIT_REVALIDATE_MAX } from "@/lib/layout/switch-budget";
import type {
  AgentInspectLive,
  AgentInspectStatistics,
  PageRow,
} from "@/lib/contracts/types";
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
  rosterFixture,
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

describe("InspectorPanel desktop", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(true);
  });

  it("uses the shared scroll surface and resets its viewport on agent switch", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container, rerender } = rtlRender(
      <QueryClientProvider client={qc}><InspectorPanel agentId={1} /></QueryClientProvider>,
    );
    const surface = container.querySelector('[data-slot="scroll-area"]');
    const viewport = surface?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]');
    expect(surface).toBeTruthy();
    expect(viewport).toBeTruthy();
    viewport!.scrollTop = 120;
    rerender(
      <QueryClientProvider client={qc}><InspectorPanel agentId={2} /></QueryClientProvider>,
    );
    expect(viewport!.scrollTop).toBe(0);
  });

  it("fills its resizable side panel without an overlay or backdrop", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const aside = screen.getByRole("complementary");
    const classes = aside.className.split(" ");
    expect(classes).toContain("flex");
    expect(classes).toContain("w-full");
    expect(classes).not.toContain("border-l");
    expect(classes).not.toContain("fixed");
    expect(classes).not.toContain("absolute");
    expect(document.querySelector('div[aria-hidden="true"]')).toBeNull();
  });

  it("uses the compact shared title bar with an inset divider", () => {
    render(<InspectorPanel agentId={1} />);

    const header = screen.getByText("Inspector", { selector: "span" }).closest("header");
    expect(header?.className).toContain(BAR_HEIGHT_CLASS);
    for (const dividerClass of BAR_DIVIDER_CLASS.split(" ")) {
      expect(header?.className).toContain(dividerClass);
    }
    expect(header?.classList.contains("relative")).toBe(true);
    expect(header?.className).not.toContain("border-b");
  });

  it("has no X close — the top-bar toggle is the desktop close (QA sweep 2026-09-18 F2)", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Close inspector" })).toBeNull();
  });

  it("Escape does not close the panel on desktop", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.keyDown(window, { key: "Escape" });
    expect(toggle).not.toHaveBeenCalled();
  });

  it("does not close when clicking outside the desktop side panel", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.mouseDown(document.body);
    expect(toggle).not.toHaveBeenCalled();
  });

  it("renders nothing while closed", () => {
    panelState.open = false;
    const { container } = render(<InspectorPanel agentId={1} />);
    expect(container.querySelector("aside")).toBeNull();
    expect(getAgentInspectStatistics).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });
});

describe("InspectorPanel mobile", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(false);
  });

  it("renders as a full-screen overlay on mobile", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByLabelText("Close inspector")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const aside = screen.getByRole("complementary");
    expect(aside.parentElement?.className.split(" ")).toEqual(
      expect.arrayContaining(["fixed", "inset-0", "z-50", "flex"]),
    );
    expect(document.querySelector('div[aria-hidden="true"]')).toBeTruthy();
  });

  it("closes when clicking the backdrop", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const backdrop = document.querySelector('[aria-hidden="true"]');
    expect(backdrop).toBeTruthy();
    fireEvent.click(backdrop!);
    expect(toggle).toHaveBeenCalled();
  });

  it("closes when clicking the X button (task #793 — the mobile overlay must be closable)", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const btn = screen.getByRole("button", { name: "Close inspector" });
    fireEvent.click(btn);
    expect(toggle).toHaveBeenCalled();
  });

  it("header shows no agent id — the timeline header already shows it (task #709)", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const header = screen.getByText("Inspector", { selector: "span" });
    expect(header.textContent).toBe("Inspector");
  });

  it("renders nothing while closed (overlay removed)", () => {
    panelState.open = false;
    const { container } = render(<InspectorPanel agentId={1} />);
    expect(container.querySelector("aside")).toBeNull();
    expect(getAgentInspectStatistics).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });

  it("multiple hidden inspector observers issue zero inspect requests", () => {
    panelState.open = false;
    render(
      <>
        <InspectorPanel agentId={1} />
        <InspectorPanel agentId={1} />
      </>,
    );
    expect(getAgentInspectStatistics).not.toHaveBeenCalled();
    expect(getAgentInspectLive).not.toHaveBeenCalled();
  });

  it("multiple open inspector observers share one initial request", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(
      <>
        <InspectorPanel agentId={1} />
        <InspectorPanel agentId={1} />
      </>,
    );

    await waitFor(() => expect(screen.getAllByText("Persistent shells")).toHaveLength(2));
    expect(getAgentInspectStatistics).toHaveBeenCalledOnce();
    expect(getAgentInspectLive).toHaveBeenCalledOnce();
  });

  it("aborts both in-flight inspect requests when the panel closes", async () => {
    let windowedSignal: AbortSignal | undefined;
    let liveSignal: AbortSignal | undefined;
    getAgentInspectStatistics.mockImplementation((_agentId, _hours, signal) => {
      windowedSignal = signal;
      return new Promise<AgentInspectStatistics>(() => undefined);
    });
    getAgentInspectLive.mockImplementation((_agentId, signal) => {
      liveSignal = signal;
      return new Promise<AgentInspectLive>(() => undefined);
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(windowedSignal).toBeDefined());
    expect(liveSignal).toBeDefined();

    panelState.open = false;
    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(windowedSignal?.aborted).toBe(true));
    expect(liveSignal?.aborted).toBe(true);
  });

  it("aborts the previous agent's inspect request before switching", async () => {
    const signals = new Map<number, AbortSignal | undefined>();
    getAgentInspectStatistics.mockImplementation((agentId, _hours, signal) => {
      signals.set(agentId, signal);
      return new Promise<AgentInspectStatistics>(() => undefined);
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(signals.get(1)).toBeDefined());

    view.rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(signals.get(2)).toBeDefined());
    expect(signals.get(1)?.aborted).toBe(true);
  });
});

describe("InspectorPanel manual refresh", () => {
  it("the Activity refresh re-fires only the windowed statistics read", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const statsCallsAfterOpen = getAgentInspectStatistics.mock.calls.length;
    const liveCallsAfterOpen = getAgentInspectLive.mock.calls.length;
    const widgetCallsAfterOpen = getAgentInspectWidgets.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh cost and activity" }));
    await waitFor(() =>
      expect(getAgentInspectStatistics.mock.calls.length).toBe(statsCallsAfterOpen + 1),
    );
    expect(getAgentInspectLive.mock.calls.length).toBe(liveCallsAfterOpen);
    expect(getAgentInspectWidgets.mock.calls.length).toBe(widgetCallsAfterOpen);
  });

  it("keeps the window picker and refresh control inside the Activity section", async () => {
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());
    const section = screen.getByText("Activity").closest("section");
    expect(section).not.toBeNull();
    expect(within(section!).getByLabelText("Cost + activity window")).toBeTruthy();
    expect(
      within(section!).getByRole("button", { name: "Refresh cost and activity" }),
    ).toBeTruthy();
  });
});

describe("InspectorPanel section layout", () => {
  it("renders the run timeline entry first, then page and the runtime sections, with Activity last", async () => {
    const page = {
      id: 7,
      agent_id: 1,
      name: "task-dashboard",
      port: 4173,
      title: "Task dashboard",
      serve_dir: null,
      url: "http://gateway.test/pages/7-task-dashboard/",
      created_at: "2026-08-24T12:00:00Z",
      closed_at: null,
    } satisfies PageRow;
    listPages.mockResolvedValue([page]);
    getAgentRoster.mockResolvedValue(rosterFixture({ harness: "meiva" }));
    act(() => {
      useTimelineStore.getState().applyTokenUsage(109_600, 0, 1_000_000, 374_000, 512_000);
    });
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("Context")).toBeTruthy());

    const shells = screen.getByText("Persistent shells");
    const liveness = screen.getByText("Liveness");
    const configOverlay = screen.getByText("Configuration overlay");
    const runLink = screen.getByRole("link", { name: "Open run timeline" });
    const pageSection = screen.getByText("Page");
    const context = screen.getByText("Context");
    const activity = screen.getByText("Activity");
    const before = (first: Node, second: Node) =>
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(before(runLink, pageSection)).toBeTruthy();
    expect(before(pageSection, shells)).toBeTruthy();
    expect(before(shells, liveness)).toBeTruthy();
    expect(before(liveness, configOverlay)).toBeTruthy();
    expect(before(configOverlay, context)).toBeTruthy();
    expect(before(context, activity)).toBeTruthy();
    act(() => { useTimelineStore.getState().applyTokenUsage(0, 0, 0, 0, 0); });
    listPages.mockResolvedValue([]);
  });

  it("embedded: renders without the header bar or the category selector", async () => {
    render(<InspectorPanel agentId={1} embedded />);
    await waitFor(() => expect(screen.getByText("Activity")).toBeTruthy());

    expect(screen.queryByText("Inspector", { selector: "span" })).toBeNull();
    expect(screen.queryByLabelText("Inspector category")).toBeNull();
    const runLink = screen.getByRole("link", { name: "Open run timeline" });
    const costActivity = screen.getByText("Activity");
    expect(
      runLink.compareDocumentPosition(costActivity) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});


describe("InspectorPanel agent switch (task #1939)", () => {
  it("refetches inspect data on hot switch-back while open", async () => {
    getAgentInspectLive.mockImplementation((id) => Promise.resolve(liveFixture({ agent_id: id })));
    getAgentInspectStatistics.mockImplementation((id) => Promise.resolve(fixture({ agent_id: id })));
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5 * 60_000 } },
    });
    const { rerender } = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getAgentInspectStatistics).toHaveBeenCalledTimes(1));

    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={2} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getAgentInspectStatistics).toHaveBeenCalledTimes(2));

    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(getAgentInspectStatistics).toHaveBeenCalledTimes(3));
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
