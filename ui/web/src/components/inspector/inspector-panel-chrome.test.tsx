import { QueryClientProvider } from "@tanstack/react-query";
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
import { useStore } from "@/lib/state/store";
import type {
  AgentInspectLive,
  AgentInspectStatistics,
  PageRow,
} from "@/lib/contracts/types";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgent,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  getSettings,
  listPages,
  liveFixture,
  panelState,
  putSetting,
  render,
  resolveNotice,
  rosterFixture,
  setInspectorOpen,
  setupInspectorTest,
  streamHandlers,
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
    putSetting,
    getSettings,
  },
}));

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

import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel desktop", () => {
  beforeEach(() => {
    window.innerWidth = 1024;
  });

  it("uses the shared scroll surface and resets its viewport on agent switch", () => {
    const qc = createInspectorQueryClient();
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
    expect(putSetting).not.toHaveBeenCalled();
  });

  it("does not close when clicking outside the desktop side panel", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    fireEvent.mouseDown(document.body);
    expect(putSetting).not.toHaveBeenCalled();
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
    window.innerWidth = 375;
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
    expect(useStore.getState().mobileInspectorOpen).toBe(false);
  });

  it("closes when clicking the X button (task #793 — the mobile overlay must be closable)", async () => {
    getAgentInspectStatistics.mockResolvedValue(fixture());
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const btn = screen.getByRole("button", { name: "Close inspector" });
    fireEvent.click(btn);
    expect(useStore.getState().mobileInspectorOpen).toBe(false);
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
    const qc = createInspectorQueryClient();
    const view = rtlRender(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(windowedSignal).toBeDefined());
    expect(liveSignal).toBeDefined();

    act(() => {
      setInspectorOpen(false, qc);
    });
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
    const qc = createInspectorQueryClient();
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
    const qc = createInspectorQueryClient({
      queries: { retry: false, staleTime: 5 * 60_000 },
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
