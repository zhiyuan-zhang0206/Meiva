// meiva:note
// HomePage tests — the surface renders from its props, so a test steers the
// values it wants through `hooksState` and watches the stand-ins `page-test-mock.tsx`
// supplies (send asks the timeline to stay with a follower, handleSend errors
// bubble to toast, composerMode tri-state). No module is replaced.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen } from "@testing-library/react";
import {  } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { LAYOUT_INVARIANTS, LAYOUT_VIEWPORT_TIERS } from "@/lib/layout/layout";
import { transport } from "@/test-support/fake-api";
import { deferred } from "@/test-support/deferred";
import { cleanupHomePageTest, HomePageBound as HomePage, hooksState, inspectorMockState, makeAgent, makeItem, resetHomePageTestState, setReplySuggestions, wrap } from "./page-test-mock";

afterEach(cleanupHomePageTest);
beforeEach(resetHomePageTestState);

describe("HomePage reply suggestion", () => {
  const agentSpoke = [
    makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "deploy?" }),
    makeItem({ item_id: "2.0", kind: "agent_chat", payload: "Deploy now?" }),
  ];
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    setReplySuggestions({ suggestions: ["Yes, go ahead.", "Not yet."], reason: null });
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    hooksState.timelineItems = agentSpoke;
    hooksState.settings = { "display.timeline_width_px": 768, "behavior.replySuggestions": true };
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
    hooksState.settings = { "display.timeline_width_px": 768, "behavior.replySuggestions": false };
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
    hooksState.timelineItems = [makeItem({ item_id: "0.1", kind: "agent_chat", payload: "read restored" })];
    view.rerender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={new QueryClient()}><HomePage /></QueryClientProvider>
      </WallClockProvider>,
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByTestId("timeline").getAttribute("data-item-count")).toBe("1");
  });
  it("shows a refresh failure without replacing loaded history, and disables Retry during its read", () => {
    hooksState.activeId = 42;
    hooksState.agents = [makeAgent({ agent_id: 42 })];
    hooksState.timelineItems = [makeItem({ item_id: "0.1", kind: "agent_chat", payload: "retained history" })];
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
    hooksState.settings = { "display.timeline_width_px": 768 };
    hooksState.inspectorOpen = true;
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
    expect(timeline.style.maxWidth).toBe("768px");
    // Floating bars sit on top of the surface as direct children (their
    // absolute + backdrop-blur classes are asserted in header-bar.test.tsx).
    const header = screen.getByTestId("header-bar");
    expect(header.parentElement).toBe(surface);
    const composer = screen.getByTestId("composer");
    expect(surface.contains(composer)).toBe(true);
  });

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
  it("content column maxWidth follows display.timeline_width_px", () => {
    hooksState.settings = { "display.timeline_width_px": 960 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("960px");
  });

  it("narrow viewport → content column full-width (no maxWidth, setting ignored)", () => {
    hooksState.isDesktop = false;
    hooksState.settings = { "display.timeline_width_px": 512 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("");
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe("undefined");
    expect(screen.getByTestId("header-bar").parentElement).toBe(
      screen.getByTestId("timeline-surface"),
    );
  });
  it("desktop viewport → content column keeps the pixel cap", () => {
    hooksState.isDesktop = true;
    hooksState.settings = { "display.timeline_width_px": 960 };
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").style.maxWidth).toBe("960px");
    expect(screen.getByTestId("composer").getAttribute("data-max-width")).toBe(
      "960px",
    );
  });

  // meiva:note
  // User ruling 2026-08-23: desktop is a side panel and mobile is a
  // full-screen overlay. Both start closed and open only via the header's
  // inspector toggle. The surface renders the column from the open flag and
  // never opens it on its own; the flag's own derivation (a workspace setting
  // on desktop, session state on mobile) is `useInspectorOpen`'s, asserted in
  // lib/inspector/inspector-panel-store.test.ts ("an explicit desktop
  // preference does not force the overlay open on mobile").
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  it("mount renders no inspector column while the inspector is closed", () => {
    hooksState.isLarge = false;
    hooksState.inspectorOpen = false;
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    wrap(<HomePage />);
    expect(screen.queryByTestId("inspector-panel")).toBeNull();
    expect(hooksState.inspectorToggle).not.toHaveBeenCalled();
  });
  it("renders an open inspector beside HomeContent in the desktop resizable group", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    hooksState.settings = { "display.timeline_width_px": 768 };
    hooksState.inspectorOpen = true;
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
    hooksState.settings = { "display.timeline_width_px": 768 };
    hooksState.inspectorOpen = true;
    const { rerender } = wrap(<HomePage />);
    await screen.findByTestId("inspector-panel");
    expect(inspectorMockState.mounts).toBe(1);
    hooksState.activeId = 6;
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={qc}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
    );
    await screen.findByTestId("inspector-panel");
    // Same mounted instance — no `key=` remount on the agent switch.
    expect(inspectorMockState.mounts).toBe(1);
  });
  it("renders ErrorBoundary fallback when inspector panel throws a render error", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 })];
    hooksState.settings = { "display.timeline_width_px": 768 };
    hooksState.inspectorOpen = true;
    inspectorMockState.shouldThrow = true;
    // meiva:note
    // This test asserts the fallback and its reset; the log the boundary and React emit on the
    // way there is theirs, so the test neither silences nor inspects it.
    wrap(<HomePage />);
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    expect(screen.getByText("Simulated inspector render crash")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
  it("resets the inspector ErrorBoundary when activeId changes via resetKey", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5 }), makeAgent({ agent_id: 6 })];
    hooksState.settings = { "display.timeline_width_px": 768 };
    hooksState.inspectorOpen = true;
    inspectorMockState.shouldThrow = true;
    const { rerender } = wrap(<HomePage />);
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    inspectorMockState.shouldThrow = false;
    hooksState.activeId = 6;
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    rerender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={qc}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
    );
    expect(await screen.findByTestId("inspector-panel")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    // Recovery rendered a fresh panel (its first successful mount); the
    // boundary reset did not tear down a healthy tree.
    expect(inspectorMockState.mounts).toBe(1);
  });
});
