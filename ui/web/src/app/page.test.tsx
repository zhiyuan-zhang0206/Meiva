// meiva:note
// HomePage tests — mock all use-* hooks and child components as stubs to test
// top-level wiring and state flow (send asks the timeline to stay with a
// follower, handleSend errors bubble to toast, composerMode tri-state).
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen } from "@testing-library/react";
import {  } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { LAYOUT_INVARIANTS, LAYOUT_VIEWPORT_TIERS } from "@/lib/layout/layout";
import { transport } from "@/test-support/fake-api";
import { deferred } from "@/test-support/deferred";
import { ComposerMock, cleanupHomePageTest, setReplySuggestions, hooksState, inspectorMockState, makeAgent, resetHomePageTestState, wrap } from "./page-test-mock";
vi.mock("@/lib/agents/use-agents", () => ({
  useAgents: () => ({
    agents: hooksState.agents,
    ancestors: [],
    activeAgent: hooksState.agents.find((a) => a.agent_id === hooksState.activeId),
    activeId: hooksState.activeId,
    setActiveId: vi.fn(),
    pendingActions: {},
    pendingSpawnCount: 0,
    spawn: hooksState.spawn,
    fork: hooksState.fork,
    terminate: hooksState.terminate,
    restart: hooksState.restart,
    resurrect: hooksState.resurrect,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/timeline/use-timeline", () => ({
  useTimeline: () => ({
    items: hooksState.timelineItems,
    streamingCode: false,
    connectionState: hooksState.connectionState,
    turnActive: hooksState.turnActive,
    isLoading: false,
    isRefetching: false,
    error: hooksState.timelineError,
    isFetching: hooksState.timelineFetching,
    retryTimeline: hooksState.retryTimeline,
  }),
}));
vi.mock("@/lib/inspector/use-token-usage", () => ({
  useTokenUsage: () => ({ contextTokens: 0, maxContextTokens: 0, softCompactTokens: 0, hardCompactTokens: 0 }),
}));
vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({ settings: hooksState.settings, setSetting: hooksState.setSetting, isLoading: false }),
}));
vi.mock("@/lib/state/store", () => ({
  useStore: <T,>(selector: (s: unknown) => T): T =>
    selector({
      toast: hooksState.toast,
      showToast: hooksState.showToast,
      focusComposer: hooksState.focusComposer,
      composerFocusToken: 0,
      setMobileSidebarOpen: hooksState.setMobileSidebarOpen,
      mobileInspectorOpen: false,
      setMobileInspectorOpen: hooksState.setMobileInspectorOpen,
      inspectorHours: 24,
      setInspectorHours: hooksState.setInspectorHours,
      agents: hooksState.agents,
      ancestors: [],
      activeAgent: hooksState.agents.find((a) => a.agent_id === hooksState.activeId),
    }),
}));
vi.mock("@/lib/layout/breakpoint", () => ({
  useBreakpoint: () => ({ tier: hooksState.isDesktop ? "xl" : "xs", isNarrow: !hooksState.isDesktop, isLarge: hooksState.isLarge }),
}));
vi.mock("@/lib/timeline/timeline-store", () => ({
  useTimelineStore: <T,>(selector: (s: unknown) => T): T =>
    selector({ requestScrollToBottom: hooksState.requestScrollToBottom }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ bfcacheId: "_b_test_" }),
}));
vi.mock("@/components/agent-sidebar", () => ({
  AgentSidebar: ({ onSpawn }: { onSpawn: (opts: { machine?: string; model?: string }) => void }) => (
    <button data-testid="sidebar-spawn" onClick={() => { onSpawn({}); }}>spawn</button>
  ),
}));
vi.mock("@/components/shell/header-bar", () => ({
  HeaderBar: ({
    label,
    children,
    maxWidthCss,
  }: {
    label: string;
    children?: React.ReactNode;
    maxWidthCss?: string;
  }) => (
    <header data-testid="header-bar" data-label={label} style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}>
      {children}
    </header>
  ),
}));
vi.mock("@/components/timeline", () => ({
  TimelineView: ({ turnActive, maxWidthCss, items }: { turnActive?: boolean; maxWidthCss?: string; items: unknown[] }) => (
    <div
      data-testid="timeline"
      data-turn-active={turnActive ? "1" : "0"}
      data-item-count={items.length}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
    />
  ),
}));
vi.mock("@/components/conversation/composer", () => ({
  Composer: ComposerMock,
}));
vi.mock("@/components/conversation/upload-button", () => ({
  UploadButton: ({ agentId }: { agentId: number | null }) => (
    <div data-testid="upload-button" data-agent-id={agentId ?? "null"} />
  ),
}));
vi.mock("@/components/inspector/inspector-panel", async () => {
  const React = await import("react");
  return {
    InspectorPanel: () => {
      React.useEffect(() => {
        inspectorMockState.mounts += 1;
      }, []);
      if (inspectorMockState.shouldThrow) {
        throw new Error("Simulated inspector render crash");
      }
      return <div data-testid="inspector-panel" />;
    },
  };
});
import HomePage from "./page";
afterEach(cleanupHomePageTest);
beforeEach(resetHomePageTestState);

describe("HomePage reply suggestion", () => {
  const agentSpoke = [
    { item_id: "1.0", kind: "inbound_chat", source: "user", payload: "deploy?" },
    { item_id: "2.0", kind: "agent_chat", source: null, payload: "Deploy now?" },
  ];
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    setReplySuggestions({ suggestions: ["Yes, go ahead.", "Not yet."], reason: null });
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    hooksState.timelineItems = agentSpoke;
    hooksState.settings = { "display.timeline_width_ratio": 0.4, "behavior.replySuggestions": true };
  });
  afterEach(() => {
    vi.useRealTimers();
  });
  // meiva:note
  // The reply is settled by hand: the debounce firing is a counted fact (`toHaveBeenCalled`),
  // and the answer lands inside `act`. No `waitFor`, so a loaded machine cannot turn this into
  // a failure, and no longer timeout can hide a regression.
  it("hands the drafted reply to the composer after the quiet period", async () => {
    const reply = deferred<{ suggestions: string[]; reason: string | null }>();
    transport.getReplySuggestion.mockImplementation(() => reply.promise);
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-suggestion")).toBe("");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    // meiva:note
    // The quiet period ended and the request went out for this agent, carrying a signal.
    expect(transport.getReplySuggestion).toHaveBeenCalledTimes(1);
    expect(transport.getReplySuggestion.mock.calls[0][0]).toBe(5);
    expect(transport.getReplySuggestion.mock.calls[0][1]).toBeInstanceOf(AbortSignal);
    await act(async () => {
      reply.resolve({ suggestions: ["Yes, go ahead.", "Not yet."], reason: null });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("composer").getAttribute("data-suggestion")).toBe("Yes, go ahead.|Not yet.");
  });
  it("never asks while the agent is running", async () => {
    hooksState.agents = [makeAgent({ agent_id: 5, status: "running" })];
    wrap(<HomePage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(transport.getReplySuggestion).not.toHaveBeenCalled();
  });
  it("never asks while the turn signal is active", async () => {
    hooksState.turnActive = true;
    wrap(<HomePage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(transport.getReplySuggestion).not.toHaveBeenCalled();
  });
  it("never asks when the user turned the setting off", async () => {
    hooksState.settings = { "display.timeline_width_ratio": 0.4, "behavior.replySuggestions": false };
    wrap(<HomePage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(transport.getReplySuggestion).not.toHaveBeenCalled();
  });
});

describe("HomePage top-level render", () => {
  it("keeps a cold checkpoint failure visible with manual Retry until a successful read", () => {
    hooksState.activeId = 42;
    hooksState.agents = [makeAgent({ agent_id: 42 })];
    hooksState.timelineError = new Error("Checkpoint history unavailable for agent 42");
    const view = wrap(<HomePage />);
    expect(screen.getByRole("alert").textContent).toContain("Could not load this conversation.");
    expect(screen.getByRole("alert").textContent).toContain("Checkpoint history unavailable for agent 42");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(hooksState.retryTimeline).toHaveBeenCalledOnce();
    hooksState.timelineError = null;
    hooksState.timelineItems = [{ item_id: "0.1", kind: "agent_chat", payload: "read restored" }];
    view.rerender(<QueryClientProvider client={new QueryClient()}><HomePage /></QueryClientProvider>);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByTestId("timeline").getAttribute("data-item-count")).toBe("1");
  });
  it("shows a refresh failure without replacing loaded history, and disables Retry during its read", () => {
    hooksState.activeId = 42;
    hooksState.agents = [makeAgent({ agent_id: 42 })];
    hooksState.timelineItems = [{ item_id: "0.1", kind: "agent_chat", payload: "retained history" }];
    hooksState.timelineError = new Error("Checkpoint history unavailable for agent 42");
    hooksState.timelineFetching = true;
    wrap(<HomePage />);
    expect(screen.getByRole("alert").textContent).toContain("Previously loaded messages are still shown.");
    expect(screen.getByTestId("timeline").getAttribute("data-item-count")).toBe("1");
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry.hasAttribute("disabled")).toBe(true);
    fireEvent.click(retry);
    expect(hooksState.retryTimeline).not.toHaveBeenCalled();
  });
  it("activeId=null → composer 'disabled' + label '…'", () => {
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("disabled");
    expect(screen.getByTestId("header-bar").getAttribute("data-label")).toBe("…");
  });
  it("activeId not null + agent in list → label contains #N + label (status moved to the inspector, task #3904)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, label: "research", status: "idling" }),
    ];
    wrap(<HomePage />);
    const label = screen.getByTestId("header-bar").getAttribute("data-label");
    expect(label).toContain("Agent #5");
    expect(label).toContain("research");
    expect(label).not.toContain("Idling");
    // meiva:note
    // The harness belongs to the inspector's Liveness section, not to the title bar:
    // naming it there was information the reader never acts on (user 2026-10-21).
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    expect(label).not.toContain("Meiva");
  });
  it("keeps the agent label while Inspector mode is on", () => {
    // meiva:note
    // Inspector changes what the right column shows, not which conversation the
    // reader is in: a bar titled "Inspector" made the agent behind it disappear
    // (user 2026-10-21).
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    hooksState.activeId = 5;
    hooksState.isLarge = true;
    hooksState.settings = { "display.timeline_width_ratio": 0.4, "display.inspector_open": true };
    hooksState.agents = [makeAgent({ agent_id: 5, label: "research", status: "idling" })];
    wrap(<HomePage />);
    const label = screen.getByTestId("header-bar").getAttribute("data-label");
    expect(label).toContain("Agent #5");
    expect(label).not.toContain("Inspector");
  });
  it("toast is NOT rendered by the page (the renderer moved to the root ToastHost in Providers, Task #1051)", () => {
    hooksState.toast = "Save failed: 500";
    wrap(<HomePage />);
    expect(screen.queryByText("Save failed: 500")).toBeNull();
  });

  // Layout regression (task #714 + #715): the timeline+composer content
  // column is width-capped and centered (mx-auto), so on wide screens the
  // conversation leaves responsive gutters on both sides — Claude/Gemini
  // style — instead of stretching full-width. The cap comes from the
  // display.timeline_width_ratio user setting as an inline maxWidth
  // (min(<ratio>vw, 1280px)).
  // User ruling 2026-08-06: the timeline is the MAIN page — the scroll
  // surface is full-bleed (scrollbar at the pane edge, gutters scrollable)
  // and HeaderBar + the composer stack float above it (absolute + backdrop
  // blur). The width cap now lives on the timeline's inner content column,
  // the HeaderBar's inner wrapper, and the composer stack's inner wrappers
  // (all mx-auto). jsdom can't measure layout, so assert the class +
  // inline-style contract on the timeline surface and the floating bars.
  it("timeline surface is full-bleed; content column capped and centered", () => {
    wrap(<HomePage />);
    const surface = screen.getByTestId("timeline-surface");
    expect(surface.className).toContain("relative");
    expect(surface.className).toContain(FLEX_1);
    expect(surface.className).toContain(MIN_H_0);
    // The surface must BE a flex container (display:flex + column) — flex-1
    // alone only sets flex-grow/shrink/basis, and without display:flex the
    // height chain breaks: the ScrollArea grows to content height, the
    // viewport has nothing to scroll, the timeline freezes (Task #874, the
    // v0.1.30 mobile-scroll regression).
    expect(surface.className.split(" ")).toContain(FLEX);
    expect(surface.className.split(" ")).toContain(FLEX_COL);
    // min-w-0: the surface is a flex item of the page section. Without it,
    // min-width:auto resolves to the composer footer's min-content (~700px),
    // so on a mobile viewport the surface lays out wider than its
    // overflow-hidden section and the right side is clipped with no way to
    // scroll to it (Task #979 — the v0.1.39 mobile right-truncation report).
    expect(surface.className.split(" ")).toContain(MIN_W_0);
    // Timeline renders inside the surface, not in a capped column.
    expect(surface.contains(screen.getByTestId("timeline"))).toBe(true);
    // Timeline content column: capped + centered inside the scroll viewport.
    const timeline = screen.getByTestId("timeline");
    expect(timeline.style.maxWidth).toBe("min(40vw, 1280px)");
    // Floating bars sit on top of the surface as direct children (their
    // absolute + backdrop-blur classes are asserted in header-bar.test.tsx).
    const header = screen.getByTestId("header-bar");
    expect(header.parentElement).toBe(surface);
    const composer = screen.getByTestId("composer");
    expect(surface.contains(composer)).toBe(true);
  });

  // Task #715: the column cap follows the display.timeline_width_ratio
  // setting (viewport fraction, clamped by lib/timeline-width.ts).
  // R4 (Task #1024): the jsdom class-contract layer covers every timeline
  // invariant in LAYOUT_INVARIANTS (the shared checklist with the
  // Playwright layer). The timeline-surface classes asserted above are the
  // raw material of I1 (no page scroll), I2 (surface not wider than parent),
  // I3 (composer fits) and I6 (single scroll region) — this guard fails when
  // someone adds an invariant to lib/layout.ts without a jsdom contract for
  // the timeline page.
  it("jsdom contract covers every timeline invariant in LAYOUT_INVARIANTS", () => {
    const timelineInvariants = LAYOUT_INVARIANTS.filter((i) => i.pages.includes("timeline"));
    expect(timelineInvariants.map((i) => i.id).sort()).toEqual(["I1", "I2", "I3", "I6"]);
    // The contract classes are the six primitives — the surface carries all of them.
    wrap(<HomePage />);
    const surface = screen.getByTestId("timeline-surface");
    const classes = surface.className.split(" ");
    for (const c of [FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0]) {
      expect(classes).toContain(c);
    }
    // Three viewport tiers are the engine-test contract (asserted for real in
    // tests/e2e/visual/test_layout_invariants.py); keep them pinned here so the
    // shared checklist cannot drift.
    expect(LAYOUT_VIEWPORT_TIERS).toEqual([320, 390, 768]);
  });
  it("content column maxWidth follows display.timeline_width_ratio", () => {
    hooksState.settings = { "display.timeline_width_ratio": 0.6 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("min(60vw, 1280px)");
  });

  // Task #805 (user ruling): the width ratio is a DESKTOP concept. On narrow
  // viewports (<768px) the timeline content column renders FULL-WIDTH — no
  // inline maxWidth at all — even when a ratio is stored in settings. The
  // composer gets the same treatment via a falsy maxWidthCss (asserted in
  // composer.test.tsx).
  it("narrow viewport → content column full-width (no maxWidth, ratio ignored)", () => {
    hooksState.isDesktop = false;
    hooksState.settings = { "display.timeline_width_ratio": 0.2 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("");
    // One falsy convention (audit C2): the composer also gets undefined on
    // narrow viewports — never "" — so a `!= null` check can't cap mobile.
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe("undefined");
    // Floating bars stay direct children of the surface on narrow viewports.
    expect(screen.getByTestId("header-bar").parentElement).toBe(
      screen.getByTestId("timeline-surface"),
    );
  });
  it("desktop viewport → content column keeps the ratio cap", () => {
    hooksState.isDesktop = true;
    hooksState.settings = { "display.timeline_width_ratio": 0.6 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("min(60vw, 1280px)");
    // The composer rides the same cap as the timeline column (#723-⑧).
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe(
      "min(60vw, 1280px)",
    );
  });

  // User ruling 2026-08-23: desktop is a side panel and mobile is a
  // full-screen overlay. Both start closed and open only via the header's
  // inspector toggle — mount never touches either open-state source.
  it("mount does not auto-open the inspector", () => {
    hooksState.isLarge = false;
    hooksState.settings = { "display.inspector_open": false };
    wrap(<HomePage />);
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    expect(hooksState.setMobileInspectorOpen).not.toHaveBeenCalled();
  });
  it("mobile mount with the setting open still does not open the overlay (task #793)", () => {
    hooksState.isLarge = false;
    hooksState.settings = { "display.inspector_open": true };
    wrap(<HomePage />);
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    expect(hooksState.setMobileInspectorOpen).not.toHaveBeenCalled();
  });
  it("renders an open inspector beside HomeContent in the desktop resizable group", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    hooksState.settings = {
      "display.timeline_width_ratio": 0.4,
      "display.inspector_open": true,
    };
    wrap(<HomePage />);

    // v4 nests each Panel: the group's direct child is the sizing node
    // ([data-panel]); the element that carries the panel's className wraps the
    // content inside it.
    const main = screen.getByRole("main");
    const homeContent = screen.getByTestId("timeline-surface").closest("section");
    const panel = await screen.findByTestId("inspector-panel");
    const timelineGroup = homeContent?.closest('[data-slot="resizable-panel-group"]');
    const inspectorPanel = panel.closest("[data-panel]");
    expect(timelineGroup).toBeTruthy();
    expect(main.contains(timelineGroup!)).toBe(true);
    expect(inspectorPanel?.parentElement).toBe(timelineGroup);
    expect(inspectorPanel?.previousElementSibling?.getAttribute("role")).toBe("separator");
  });
  it("keeps a healthy inspector mounted across an agent switch (no remount, task #3894)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 }), makeAgent({ agent_id: 6 })];
    hooksState.settings = {
      "display.timeline_width_ratio": 0.4,
      "display.inspector_open": true,
    };
    const { rerender } = wrap(<HomePage />);
    await screen.findByTestId("inspector-panel");
    expect(inspectorMockState.mounts).toBe(1);
    hooksState.activeId = 6;
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <QueryClientProvider client={qc}>
        <HomePage />
      </QueryClientProvider>,
    );
    await screen.findByTestId("inspector-panel");
    // Same mounted instance — no `key=` remount on the agent switch.
    expect(inspectorMockState.mounts).toBe(1);
  });
  it("renders ErrorBoundary fallback when inspector panel throws a render error", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    hooksState.settings = {
      "display.timeline_width_ratio": 0.4,
      "display.inspector_open": true,
    };
    inspectorMockState.shouldThrow = true;
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {
      /* silence error logs in test */
    });
    wrap(<HomePage />);
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    expect(screen.getByText("Simulated inspector render crash")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    consoleSpy.mockRestore();
  });
  it("resets the inspector ErrorBoundary when activeId changes via resetKey", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 }), makeAgent({ agent_id: 6 })];
    hooksState.settings = {
      "display.timeline_width_ratio": 0.4,
      "display.inspector_open": true,
    };
    inspectorMockState.shouldThrow = true;
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {
      /* silence error logs in test */
    });
    const { rerender } = wrap(<HomePage />);
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    inspectorMockState.shouldThrow = false;
    hooksState.activeId = 6;
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <QueryClientProvider client={qc}>
        <HomePage />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("inspector-panel")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    // Recovery rendered a fresh panel (its first successful mount); the
    // boundary reset did not tear down a healthy tree.
    expect(inspectorMockState.mounts).toBe(1);
    consoleSpy.mockRestore();
  });
});
