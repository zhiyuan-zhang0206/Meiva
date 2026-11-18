import { useLayoutEffect, useMemo, useRef, type ComponentProps } from "react";
import { readScrollMemory, saveScrollMemory } from "@/lib/layout/scroll-memory";
import { timelineGeometry } from "@/test-support/timeline-geometry";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupHomePageTest, gatewayWrapper, HomePageView, homePageProps, HomePageBound as HomePage, hooksState, makeAgent, resetHomePageTestState, wrap } from "./page-test-mock";

import { resetTransport, transport } from "@/test-support/fake-api";
import { preloadChatMarkdown } from "@/components/content/markdown";
import { Composer } from "@/components/conversation/composer";
import { TimelineView } from "@/components/timeline";
import { fakeBrowser } from "@/lib/browser/testing";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type TimelineResponse, type ConversationSnapshotResponse } from "@/lib/contracts/types";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { fakeStreamHub, TestStreamProvider } from "@/test-support/fake-stream";
import { deferred } from "@/test-support/deferred";
import { httpRig, noUpload, sequentialKeys } from "@/lib/transport/tests/api/api-provider";

function composed(timeline: TimelineResponse): ConversationSnapshotResponse {
  return { timeline, pending: [], token_usage: { input_tokens: 0, output_tokens: 0,
    reasoning_tokens: 0, max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0 } };
}

afterEach(cleanupHomePageTest);
beforeEach(resetHomePageTestState);

describe("sidebar app-entry reset", () => {
  it("resets a loaded collapsed preference when the home surface is entered", () => {
    hooksState.sidebarCollapsed = true;
    wrap(<HomePage />);
    expect(hooksState.setSetting).toHaveBeenCalledWith(
      "display.sidebar_collapsed",
      false,
    );
  });
  it("preserves a collapse that happens after the home surface has mounted", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const view = renderWithBrowser(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={queryClient}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
      { wrapper: gatewayWrapper },
    );
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    hooksState.sidebarCollapsed = true;
    view.rerender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={queryClient}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
    );
    expect(hooksState.setSetting).not.toHaveBeenCalled();
  });
});

describe("home loading presentation through the selected timeline reader", () => {
  const human: BackendTimelineItem = { item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Selected prompt", created_at: null, inbound_id: 1, show_timestamp: false };
  const { hub } = fakeStreamHub();
  let client: QueryClient;
  let visit = 0;
  const response = (items: BackendTimelineItem[] = [], has_more = false): TimelineResponse => ({ items, msg_count: items.length, has_more });

  function Reader({ id = 42, running = true }: { id?: number; running?: boolean }) {
    const timeline = useTimeline(id, () => undefined, running ? "running" : "idling");
    const props = homePageProps();
    const agent = makeAgent({ agent_id: id, status: running ? "running" : "idling" });
    return <HomePageView {...props} agents={{ ...props.agents, activeId: id, activeAgent: agent, agents: [agent] }}
      timeline={timeline} TimelineView={TimelineView} bfcacheId={`home-loading-${visit}`} />;
  }

  function surface(ui: React.ReactNode) {
    return <QueryClientProvider client={client}><ApiProvider api={transport}><StreamHubProvider hub={hub}>
      <WallClockProvider wallNow={Date.now}><TestStreamProvider>{ui}</TestStreamProvider></WallClockProvider>
    </StreamHubProvider></ApiProvider></QueryClientProvider>;
  }

  beforeEach(() => {
    visit += 1;
    client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false });
    client.setQueryData(["config", null], { fields: [] });
    transport.getConfig.mockResolvedValue({ fields: [] });
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getAgentQuestions.mockResolvedValue([]);
    useTimelineStore.getState().switchThread(null, null, false);
  });
  afterEach(() => { cleanup(); client.clear(); });

  it("retained rows await initial qualification under one ring through the home prop boundary", () => {
    const props = homePageProps();
    const agent = makeAgent({ agent_id: 42, status: "running" });
    const timeline = { ...props.timeline, items: [human], isLoading: true, loadingOlder: true, readerLoadingOlder: true };
    const view = renderWithBrowser(surface(<HomePageView {...props} agents={{ ...props.agents, activeId: 42, activeAgent: agent }}
      timeline={timeline} TimelineView={TimelineView} />));
    expect(screen.getByText("Selected prompt")).toBeTruthy();
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(1);
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
  });

  it("cold entry with a running roster shows one ring until the empty read settles into Working", async () => {
    const read = deferred<TimelineResponse>();
    transport.getTimeline.mockReturnValue(read.promise);
    const view = renderWithBrowser(surface(<Reader />));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(1);
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
    expect(screen.queryByText("Working…")).toBeNull();
    await act(() => { read.resolve(response()); return read.promise; });
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    const pending = screen.getByTestId("pending-live-activity-window");
    expect(pending.textContent).toBe("Working…");
    expect(pending.querySelector("svg")).toBeTruthy();
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(0);
    view.rerender(surface(<Reader running={false} />));
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
  });

  it("a reader request suppresses the pending Working row and restores it after failure", async () => {
    client.setQueryData(["timeline", 42], response([human], true));
    const read = deferred<TimelineResponse>();
    transport.getTimeline.mockReturnValue(read.promise);
    transport.getConversationSnapshot.mockResolvedValue(composed(response([human], true)));
    const view = renderWithBrowser(surface(<Reader />));
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(screen.getByTestId("pending-live-activity-window").textContent).toBe("Working…");
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
    await waitFor(() => expect(transport.getTimeline).toHaveBeenCalledOnce());
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("loading");
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(1);
    await act(async () => { read.reject(new Error("Older fixture failed")); await Promise.resolve(); });
    await waitFor(() => expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("idle"));
    const pending = screen.getByTestId("pending-live-activity-window");
    expect(pending.textContent).toBe("Working…");
    expect(pending.querySelector(".animate-spin")).toBeNull();
    expect(screen.getByText("Selected prompt")).toBeTruthy();
  });

  it("rapid switching ignores a late old read and renders the selected work block", async () => {
    client.setQueryData(["timeline", 42], response([human]));
    const oldRead = deferred<TimelineResponse>();
    const selectedRead = deferred<TimelineResponse>();
    transport.getTimeline.mockImplementation((id) => id === 43 ? oldRead.promise : selectedRead.promise);
    const view = renderWithBrowser(surface(<Reader />));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(screen.getByText("Selected prompt")).toBeTruthy();
    view.rerender(surface(<Reader id={43} />));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    view.rerender(surface(<Reader id={44} />));
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(1);
    await act(() => { oldRead.resolve(response([{ ...human, payload: "Old source" }])); return oldRead.promise; });
    expect(screen.queryByText("Old source")).toBeNull();
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    await act(() => { selectedRead.resolve(response([human, { ...human, item_id: "2.0", kind: "agent_reasoning", source: null, payload: "Selected work", reasoning_ms: 2000 }])); return selectedRead.promise; });
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 2s");
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
  });

  it.each(["cached", "cold"])("keeps %s A2 unqualified when abandoned A1 finishes after A→B→A", async (source) => {
    if (source === "cached") client.setQueryData(["timeline", 42], response([human]));
    const first = deferred<TimelineResponse>();
    const second = deferred<TimelineResponse>();
    const other = deferred<TimelineResponse>();
    let reads = 0;
    transport.getTimeline.mockImplementation((id) => id !== 42 ? other.promise : ++reads === 1 ? first.promise : second.promise);
    let snapshots = 0;
    transport.getConversationSnapshot.mockImplementation(() =>
      (++snapshots === 1 ? first.promise : second.promise).then(composed));
    const view = renderWithBrowser(surface(<Reader running={false} />));
    const calls = () => source === "cached" ? transport.getConversationSnapshot.mock.calls : transport.getTimeline.mock.calls.filter(([id]) => id === 42);
    await waitFor(() => expect(calls()).toHaveLength(1));
    view.rerender(surface(<Reader id={43} running={false} />));
    view.rerender(surface(<Reader running={false} />));
    await waitFor(() => expect(calls()).toHaveLength(2));
    await act(() => { first.resolve(response([{ ...human, payload: "Abandoned A1" }])); return first.promise; });
    expect(screen.queryByText("Abandoned A1")).toBeNull();
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    await act(() => { second.resolve(response([human])); return second.promise; });
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(screen.getByText("Selected prompt")).toBeTruthy();
  });

  it("a failed cold read releases the ring and exposes the existing retry action", async () => {
    const read = deferred<TimelineResponse>();
    transport.getTimeline.mockReturnValue(read.promise);
    const view = renderWithBrowser(surface(<Reader running={false} />));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    await act(async () => { read.reject(new Error("Selected source failed")); await Promise.resolve(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy());
    expect(view.container.querySelectorAll(".animate-spin")).toHaveLength(0);
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
  });
});

describe("composerMode derivation", () => {
  it("activeId not null + status='terminated' → idle (send auto-resurrects)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "terminated" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });
  it("activeId + turnActive=true → busy", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = true;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });
  it("activeId + not terminated + turnActive=false → idle", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "idling" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });
  it("status='running' + turnActive=false → busy (Stop available between actions)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });
  it("TimelineView receives busy=true while status='running' even when turnActive=false", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "running" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("1");
  });
  it("TimelineView receives busy=false when idle and no turn in flight", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("0");
  });
});

describe("Spawn flow", () => {
  it("spawn succeeds → focusComposer is called", async () => {
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.focusComposer).toHaveBeenCalled());
  });
  it("spawn returns null → focusComposer not called", async () => {
    hooksState.spawn = vi.fn().mockResolvedValue(null);
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.spawn).toHaveBeenCalled());
    expect(hooksState.focusComposer).not.toHaveBeenCalled();
  });
});

// meiva:note
// A send hands the timeline the message the gateway stored, so it can park that
// message instead of re-pinning to the bottom; the view does the placing.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.md
describe("send hands the timeline its own message", () => {
  it("send succeeds → requestSendAnchor carries the stored inbound", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() =>
      expect(transport.sendMessage).toHaveBeenCalledWith(5, "hi", expect.any(String), undefined),
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestSendAnchor).toHaveBeenCalledWith(11);
  });
  it("send fails → requestSendAnchor NOT called", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    transport.sendMessage.mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestSendAnchor).not.toHaveBeenCalled();
  });
});

describe("UploadButton receives activeId", () => {
  it("activeId=null → upload-button data-agent-id 'null'", () => {
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("null");
  });
  it("activeId=7 → upload-button data-agent-id '7'", () => {
    hooksState.activeId = 7;
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("7");
  });
});

describe("model capability notice & unified file staging", () => {
  it("explains unknown model capabilities while text, stop and file staging remain available", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, supports_vision: null, status: "running" })];
    wrap(<HomePage />);
    expect(screen.getByText("Model capabilities are unavailable. You can still send text or request a stop.")).toBeTruthy();
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
    fireEvent.click(screen.getByTestId("composer-send-plain"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId("composer-stop"));
    await waitFor(() => expect(transport.interrupt).toHaveBeenCalledWith(5));
    expect(hooksState.agents[0].status).toBe("running");
    fireEvent.click(screen.getByTestId("upload-button"));
    expect(screen.getByTestId("composer").getAttribute("data-stage-seq")).toBe("1");
    expect(screen.getByTestId("composer").getAttribute("data-stage-files")).toBe("picked.txt");
  });

  it.each(["claude_code", "codex"] as const)("keeps file staging for %s harnesses", (harness) => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, harness, supports_vision: true })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("upload-button"));
    expect(screen.getByTestId("composer").getAttribute("data-stage-files")).toBe("picked.txt");
  });
});

describe("multi-command dispatch", () => {
  it("sends one message for two space-separated commands", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "/compact /update",
      expect.any(String),
      undefined,
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestSendAnchor).toHaveBeenCalledTimes(1);
  });
  it("sends command arguments verbatim in that one message", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi-args"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "/search hello world /compact",
      expect.any(String),
      undefined,
    );
  });
  it("sends a single message for plain text (no slash prefix)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-plain"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "plain text",
      expect.any(String),
      undefined,
    );
  });
  it("does not ask for the tail when the send fails", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    transport.sendMessage.mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestSendAnchor).not.toHaveBeenCalled();
  });
});

// meiva:note
// Keep the actual home consumer in the request-to-ring path: connecting the hook
// directly to TimelineView would bypass HomeContent's prop forwarding.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
describe("home older-history request presentation", () => {
const items: BackendTimelineItem[] = Array.from({ length: 16 }, (_, i) => ({
  item_id: `${i + 1}.0`, kind: "agent_chat", payload: `Reply ${i + 1}`,
  source: null, created_at: null, inbound_id: null, show_timestamp: false,
}));
const { hub } = fakeStreamHub();
let client: QueryClient;
let top = 0;
let visit = 0;

function HomeReader() {
  const timeline = useTimeline(42, () => undefined);
  return <><button type="button" onClick={() => timeline.loadOlder("background")}>Background fixture read</button>
    <HomePageView {...homePageProps()} timeline={timeline} TimelineView={TimelineView} /></>;
}

beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
  transport.getPendingMessages.mockResolvedValue([]);
  transport.getUserMessages.mockResolvedValue({ mode: "prefix", seen_id: 0, items: [] });
  transport.getAgentQuestions.mockResolvedValue([]);
  hooksState.bfcacheId = `home-paging-${++visit}`;
  hooksState.activeId = 42;
  hooksState.agents = [makeAgent({ agent_id: 42, status: "idling" })];
  hooksState.settings = { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false };
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["timeline", 42], { items, msg_count: 17, has_more: true });
  transport.getConversationSnapshot.mockResolvedValue(composed({ items, msg_count: 17, has_more: true }));
  client.setQueryData(["user-settings"], hooksState.settings);
  client.setQueryData(["config", null], { fields: [] });
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
});
afterEach(() => { cleanup(); client.clear(); });

  it.each(["reader", "background"])("presents %s reads through the home consumer and shows a reader retry after failure", async (origin) => {
    let fail: (reason: Error) => void = () => undefined;
    transport.getTimeline.mockImplementation(() => new Promise<TimelineResponse>((_, reject) => { fail = reject; }));
    renderWithBrowser(<QueryClientProvider client={client}>
      <ApiProvider api={transport}><StreamHubProvider hub={hub}><WallClockProvider wallNow={Date.now}>
        <TestStreamProvider><HomeReader /></TestStreamProvider>
      </WallClockProvider></StreamHubProvider></ApiProvider>
    </QueryClientProvider>);
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    const viewport = document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
    Object.defineProperties(viewport, {
      clientHeight: { configurable: true, get: () => 600 },
      scrollHeight: { configurable: true, get: () => 5000 },
      scrollTop: { configurable: true, get: () => top,
        set: (value: number) => { top = Math.max(0, Math.min(4400, value)); } },
    });
    for (const element of [viewport, ...viewport.querySelectorAll<HTMLElement>("[data-item-id]")]) {
      Object.defineProperty(element, "getBoundingClientRect", {
        configurable: true,
        value: () => {
          const y = element.dataset.itemId ? (Number.parseInt(element.dataset.itemId) - 1) * 300 - top : 0;
          return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
        },
      });
    }
    act(() => { viewport.scrollTop = 4400; fireEvent.resize(window); fireEvent.scroll(viewport); });
    const wheel = (at: number) => {
      const event = new WheelEvent("wheel", { deltaY: -80, bubbles: true, cancelable: true });
      Object.defineProperty(event, "timeStamp", { value: at });
      fireEvent(viewport, event);
    };
    wheel(1000);
    act(() => { viewport.scrollTop = 0; fireEvent.scroll(viewport); });
    wheel(1040);
    expect(transport.getTimeline).not.toHaveBeenCalled();
    if (origin === "reader") wheel(1500);
    else fireEvent.click(screen.getByRole("button", { name: "Background fixture read" }));
    await waitFor(() => expect(transport.getTimeline).toHaveBeenCalledOnce());
    expect(transport.getTimeline).toHaveBeenCalledWith(42, expect.objectContaining({ before: "1.0" }));
    expect(useTimelineStore.getState().loadingOlder).toBe(true);
    const strip = screen.getByTestId("load-older-spinner");
    expect(strip.dataset.state).toBe(origin === "reader" ? "loading" : "idle");
    const ring = strip.querySelector(".animate-spin");
    if (origin === "reader") expect(ring).not.toBeNull();
    else expect(ring).toBeNull();
    wheel(1800);
    expect(transport.getTimeline).toHaveBeenCalledOnce();
    expect(strip.querySelector(".animate-spin")).toBe(ring);
    await act(async () => { fail(new Error("fixture page failure")); await Promise.resolve(); });
    expect(strip.dataset.state).toBe("idle");
    wheel(2200);
    await waitFor(() => expect(transport.getTimeline).toHaveBeenCalledTimes(2));
    expect(strip.dataset.state).toBe("loading");
  });

});


describe("home return while the source window loads", () => {
const items: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
  item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
  source: null, created_at: null, inbound_id: null, show_timestamp: false,
}));
const response: TimelineResponse = { items, msg_count: 17, has_more: false };
const gateway = transport;
const wallNow = () => Date.now();
let client: QueryClient;
let top = 0;
let geometry: ReturnType<typeof timelineGeometry>;
let resizeCallbacks: Set<() => void>;
function viewport() { return document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!; }
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }
function readingOffset(id: string) {
  const node = viewport().querySelector(`.timeline-item[data-item-id="${id}"]`);
  return node ? Math.round(node.getBoundingClientRect().top - viewport().getBoundingClientRect().top) : null;
}
beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["timeline", 42], response);
  client.setQueryData(["timeline", 43], response);
  transport.getConversationSnapshot.mockResolvedValue(composed(response));
  client.setQueryData(["config", null], { fields: [] });
  client.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS });
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
  resizeCallbacks = new Set();
  geometry = timelineGeometry({
    top: () => top, setTop: (value) => { top = value; },
    height: () => 5000, resizeCallbacks,
  });
});
afterEach(() => {
  cleanup(); client.clear();
});

function MeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { if (root.current) geometry.prepare(root.current); });
  return <div ref={root}><TimelineView {...props} viewportObserverFactory={geometry.observer} /></div>;
}

function FreshMeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  const root = useRef<HTMLDivElement>(null);
  const ownTop = useRef(0);
  const measured = useMemo(() => timelineGeometry({
    top: () => ownTop.current, setTop: (value) => { ownTop.current = value; },
    height: () => 5000, resizeCallbacks,
  }), []);
  useLayoutEffect(() => { if (root.current) measured.prepare(root.current); });
  return <div ref={root}><TimelineView {...props} viewportObserverFactory={measured.observer} /></div>;
}

  it.each(["cold", "retained"])("keeps the departed bookmark through %s source layouts", async (source) => {
    hooksState.activeId = 42;
    hooksState.agents = [makeAgent({ agent_id: 42 })];
    hooksState.bfcacheId = `home-loading-return-${source}`;
    hooksState.settings = { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false };
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getUserMessages.mockResolvedValue({ mode: "prefix", seen_id: 0, items: [] });
    transport.getAgentQuestions.mockResolvedValue([]);
    const { hub } = fakeStreamHub();
    function HomeReader() {
      const timeline = useTimeline(hooksState.activeId, () => undefined);
      return <HomePageView {...homePageProps()} timeline={timeline} TimelineView={MeasuredTimeline} />;
    }
    const home = () => <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}><ApiProvider api={gateway}>
        <StreamHubProvider hub={hub}><TestStreamProvider><HomeReader /></TestStreamProvider></StreamHubProvider>
      </ApiProvider></WallClockProvider>
    </QueryClientProvider>;
    const view = renderWithBrowser(home());
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    hooksState.activeId = 43;
    view.rerender(home());
    expect(readScrollMemory(`home-loading-return-${source}:agent:42`)?.scrollTop).toBe(700);
    if (source === "cold") act(() => {
      client.removeQueries({ queryKey: ["timeline", 42], exact: true });
      client.removeQueries({ queryKey: ["timeline-reading-window", 42], exact: true });
    });
    let deliver: (value: TimelineResponse) => void = () => undefined;
    transport.getTimeline.mockImplementation(() => new Promise<TimelineResponse>((resolve) => { deliver = resolve; }));
    hooksState.activeId = 42;
    view.rerender(home());
    if (source === "cold") {
      await waitFor(() => expect(transport.getTimeline).toHaveBeenCalled());
      resize(); resize(); resize();
      await act(async () => { deliver(response); await Promise.resolve(); });
    }
    await waitFor(() => expect(viewport().querySelectorAll(".timeline-item")).toHaveLength(16));
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    resize();
    expect(viewport().scrollTop).toBe(700);
    expect(readingOffset("3.0")).toBe(-100);
  });
  it.each(["unchanged", "new", "empty", "error", "no-saved"])("qualifies cached Home only after this entry's %s authority settles", async (outcome) => {
    hooksState.activeId = 42;
    hooksState.agents = [makeAgent({ agent_id: 42, status: "idling" })];
    hooksState.bfcacheId = `authority-${outcome}`;
    hooksState.settings = { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false };
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getUserMessages.mockResolvedValue({ mode: "prefix", seen_id: 0, items: [] });
    transport.getAgentQuestions.mockResolvedValue([]);
    const entryKey = `authority-${outcome}:agent:42`;
    if (outcome !== "no-saved") saveScrollMemory(entryKey, {
      contentKey: "42", scrollTop: 700, followBottom: false,
      anchor: { itemId: "3.0", rank: 0, viewportTop: -100 },
    });
    let deliver: (value: ConversationSnapshotResponse) => void = () => undefined;
    let fail: (reason: Error) => void = () => undefined;
    transport.getConversationSnapshot.mockImplementation(() => new Promise<ConversationSnapshotResponse>((resolve, reject) => { deliver = resolve; fail = reject; }));
    transport.getTimeline.mockRejectedValue(new Error("fixture authority unavailable"));
    let current: ReturnType<typeof useTimeline> | undefined;
    const { hub } = fakeStreamHub();
    function HomeReader() {
      const timeline = useTimeline(42, () => undefined);
      current = timeline;
      return <HomePageView {...homePageProps()} timeline={timeline} TimelineView={MeasuredTimeline} />;
    }
    renderWithBrowser(<QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}><ApiProvider api={gateway}>
        <StreamHubProvider hub={hub}><TestStreamProvider><HomeReader /></TestStreamProvider></StreamHubProvider>
      </ApiProvider></WallClockProvider>
    </QueryClientProvider>);
    expect(current?.isLoading).toBe(true);
    resize(); resize(); resize();
    if (outcome !== "no-saved") expect(top).toBe(0);
    await waitFor(() => expect(transport.getConversationSnapshot).toHaveBeenCalledOnce());
    const fresh = outcome === "empty" ? { items: [], msg_count: 0, has_more: false } : outcome === "new" ? {
      items: items.map((item, index) => ({ ...item, item_id: `${100 + index}.0`, payload: `Fresh ${index}` })), msg_count: 116, has_more: true,
    } : response;
    if (outcome === "error") {
      act(() => { fail(new Error("fixture composed unavailable")); });
      await waitFor(() => expect(current?.error).toBeInstanceOf(Error));
      await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy());
      expect(current?.isLoading).toBe(false);
      resize(); resize(); resize();
      expect(top).toBe(0);
      expect(readScrollMemory(entryKey)?.scrollTop).toBe(700);
      transport.getTimeline.mockResolvedValue(response);
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    } else act(() => { deliver({
      timeline: fresh, pending: [],
      token_usage: { input_tokens: 0, output_tokens: 0, reasoning_tokens: 0,
        max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0 },
    }); });
    await waitFor(() => expect(current?.isLoading).toBe(false));
    resize();
    if (outcome === "unchanged" || outcome === "error") expect(top).toBe(700);
    if (outcome === "new") expect(current?.items.some((item) => item.payload === "Fresh 0")).toBe(true);
    const background = new Promise<TimelineResponse>(() => undefined);
    transport.getTimeline.mockReturnValue(background);
    act(() => { void client.invalidateQueries({ queryKey: ["timeline", 42] }); });
    await waitFor(() => expect(current?.isFetching).toBe(true));
    expect(current?.isLoading).toBe(false);
  });

  it("finishes seen before the cached source, then restores bottom after the actual source applies", async () => {
    const authority = deferred<ConversationSnapshotResponse>();
    const empty: TimelineResponse = { items: [], msg_count: 0, has_more: false };
    let holdAuthority = false;
    let unread = false;
    let seen = 0;
    let current: ReturnType<typeof useTimeline> | undefined;
    const trace: { event: string; sourceReady: boolean; atBottom: boolean }[] = [];
    const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
    const delivery = { id: 1, agent_id: 42, mode: "prefix", source_key: "harness:1.0", content: "Reply 1",
      suggestions: [], created_at: "2026-11-10T00:00:00Z", seen: false };
    const rig = httpRig({ base: "http://home-readiness.test", keys: sequentialKeys(), now: () => 0, openUpload: noUpload,
      fetch: (input) => {
        const url = new URL(typeof input === "string" ? input : "url" in input ? input.url : input.href);
        const path = url.pathname;
        if (path.endsWith("/conversation-snapshot")) {
          if (path.includes("/42/") && holdAuthority) return authority.promise.then(json);
          return Promise.resolve(json(composed(path.includes("/42/") ? response : empty)));
        }
        if (path.endsWith("/timeline")) return Promise.resolve(json(path.includes("/42/") ? response : empty));
        if (path.endsWith("/user-messages/seen")) { seen = 1; return Promise.resolve(json({ seen_id: 1, advanced: true })); }
        if (path.endsWith("/user-messages")) return Promise.resolve(json({ mode: "prefix", seen_id: seen, items: unread && path.includes("/42/") ? [delivery] : [] }));
        if (path.endsWith("/to-user")) return Promise.resolve(json({ content: null, version: null, modified_at: null, legacy_html: false }));
        if (path.endsWith("/pending") || path.endsWith("/questions/open")) return Promise.resolve(json([]));
        throw new Error(`Unexpected Home readiness request: ${path}`);
      },
    });
    const { hub } = fakeStreamHub();
    const entry = "seen-before-authority";
    function Reader({ id }: { id: number }) {
      const timeline = useTimeline(id, () => undefined);
      current = timeline;
      const props = homePageProps();
      const agent = makeAgent({ agent_id: id, status: "idling" });
      return <HomePageView {...props} bfcacheId={entry}
        settings={{ settings: { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false }, isLoading: false }}
        agents={{ ...props.agents, activeId: id, activeAgent: agent, agents: [agent] }}
        timeline={timeline} TimelineView={FreshMeasuredTimeline} />;
    }
    const surface = (id: number) => <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}><ApiProvider api={rig.api}>
        <StreamHubProvider hub={hub}><TestStreamProvider><Reader id={id} /></TestStreamProvider></StreamHubProvider>
      </ApiProvider></WallClockProvider>
    </QueryClientProvider>;
    const atBottom = () => Math.abs(viewport().scrollHeight - viewport().clientHeight - viewport().scrollTop) <= 1;
    const view = renderWithBrowser(surface(42));
    await waitFor(() => expect(current?.sourceReady).toBe(true));
    resize();
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    expect(atBottom()).toBe(true);
    view.rerender(surface(43));
    await waitFor(() => expect(current?.sourceReady).toBe(true));
    const saved = readScrollMemory(`${entry}:agent:42`);
    expect(saved?.scrollTop).toBe(4400);
    unread = true; holdAuthority = true;
    view.rerender(surface(42));
    await waitFor(() => expect(client.getQueryData<{ seen_id: number }>(["user-messages", 42])?.seen_id).toBe(1));
    trace.push({ event: "seen-applied", sourceReady: current?.sourceReady === true, atBottom: atBottom() });
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(readScrollMemory(`${entry}:agent:42`)).toEqual(saved);
    act(() => { authority.resolve(composed(response)); });
    await waitFor(() => expect(current?.sourceReady).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("cold-load-ring")).toBeNull());
    resize();
    trace.push({ event: "source-applied", sourceReady: current?.sourceReady === true, atBottom: atBottom() });
    expect(trace).toEqual([
      { event: "seen-applied", sourceReady: false, atBottom: false },
      { event: "source-applied", sourceReady: true, atBottom: true },
    ]);
  });

});

describe("module-level feature switches on HomePageView", () => {
  it("toggles feature.quoteSelection cleanly while keeping historical QuoteCards visible", async () => {
    await preloadChatMarkdown();
    let currentRange: Range | null = null;
    const selectionListeners = new Set<() => void>();
    const baseBrowser = fakeBrowser();
    const browser = {
      ...baseBrowser,
      selection: {
        read: () => currentRange,
        subscribe: (_root: Element, onChange: () => void) => {
          selectionListeners.add(onChange);
          return () => {
            selectionListeners.delete(onChange);
          };
        },
      },
    };
    const agent = makeAgent({ agent_id: 42, status: "idling" });
    const timelineItems: BackendTimelineItem[] = [
      {
        item_id: "1.0",
        kind: "agent_chat",
        payload: "Selectable target paragraph",
        source: null,
        created_at: "2026-11-18T10:00:00Z",
        inbound_id: null,
        source_message_id: "msg-1",
        source_block_idx: 0,
        show_timestamp: true,
        user_facing: true,
      },
      {
        item_id: "2.0",
        kind: "inbound_chat",
        payload: "Follow-up reply",
        source: "user",
        created_at: "2026-11-18T10:01:00Z",
        inbound_id: 2,
        show_timestamp: true,
        quote: {
          source_agent_id: 42,
          source_item_id: "1.0",
          author: "Agent #42",
          created_at: "2026-11-18T10:00:00Z",
          text: "Historical persisted quote",
          source_message_id: "msg-1",
          source_block_idx: 0,
        },
      },
    ];
    transport.getCommands.mockResolvedValue([]);
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getAgentQuestions.mockResolvedValue([]);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const renderHome = (quoteSelectionSetting: boolean) => {
      const props = homePageProps();
      return (
        <WallClockProvider wallNow={() => 0}>
          <QueryClientProvider client={qc}>
            <HomePageView
              {...props}
              agents={{ ...props.agents, activeId: 42, activeAgent: agent, agents: [agent] }}
              settings={{
                settings: {
                  ...USER_SETTING_DEFAULTS,
                  "behavior.replySuggestions": false,
                  "feature.quoteSelection": quoteSelectionSetting,
                },
                isLoading: false,
              }}
              timeline={{ ...props.timeline, items: timelineItems }}
              TimelineView={TimelineView}
              Composer={Composer}
            />
          </QueryClientProvider>
        </WallClockProvider>
      );
    };

    const view = renderWithBrowser(renderHome(true), { browser, wrapper: gatewayWrapper });
    await waitFor(() => expect(screen.getByText("Selectable target paragraph")).toBeTruthy());
    expect(selectionListeners.size).toBe(1);
    expect(screen.getByText("Historical persisted quote")).toBeTruthy();

    const bodyNode = view.container.querySelector('[data-item-id="1.0"] [data-quote-body]')!;
    const textNode = document.createTreeWalker(bodyNode, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, 10);
    currentRange = range;
    act(() => {
      selectionListeners.forEach((notify) => notify());
    });

    const quoteBtn = await screen.findByRole("button", { name: "Quote selection" });
    fireEvent.click(quoteBtn);
    expect((await screen.findByTestId("composer-quote")).textContent).toContain("Selectable");
    expect(browser.session.read("composer-quote-42").state).toBe("found");

    view.rerender(renderHome(false));
    await waitFor(() => expect(screen.queryByTestId("composer-quote")).toBeNull());
    expect(selectionListeners.size).toBe(0);
    expect(browser.session.read("composer-quote-42")).toEqual({ state: "absent" });
    expect(screen.queryByRole("button", { name: "Quote selection" })).toBeNull();
    expect(screen.getByText("Historical persisted quote")).toBeTruthy();

    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "Plain follow-up" } });
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() =>
      expect(transport.sendMessage).toHaveBeenCalledWith(
        42,
        "Plain follow-up",
        expect.any(String),
        undefined,
      ),
    );
  });

  it("toggles feature.markdownImageZoom on and off through HomePageView settings", async () => {
    await preloadChatMarkdown();
    const agent = makeAgent({ agent_id: 42, status: "idling" });
    const timelineItems: BackendTimelineItem[] = [
      {
        item_id: "1.0",
        kind: "agent_chat",
        payload: "![architecture](https://example.com/arch.png)",
        source: null,
        created_at: "2026-11-18T10:00:00Z",
        inbound_id: null,
        show_timestamp: true,
        user_facing: true,
      },
    ];
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getAgentQuestions.mockResolvedValue([]);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const renderHome = (zoomSetting: boolean) => {
      const props = homePageProps();
      return (
        <WallClockProvider wallNow={() => 0}>
          <QueryClientProvider client={qc}>
            <HomePageView
              {...props}
              agents={{ ...props.agents, activeId: 42, activeAgent: agent, agents: [agent] }}
              settings={{
                settings: {
                  ...USER_SETTING_DEFAULTS,
                  "behavior.replySuggestions": false,
                  "feature.markdownImageZoom": zoomSetting,
                },
                isLoading: false,
              }}
              timeline={{ ...props.timeline, items: timelineItems }}
              TimelineView={TimelineView}
            />
          </QueryClientProvider>
        </WallClockProvider>
      );
    };

    const view = renderWithBrowser(renderHome(true), { wrapper: gatewayWrapper });
    expect(await screen.findByRole("img", { name: "architecture" })).toBeTruthy();
    expect(screen.getByTestId("markdown-image-zoom")).toBeTruthy();

    view.rerender(renderHome(false));
    expect(screen.getByRole("img", { name: "architecture" })).toBeTruthy();
    expect(screen.queryByTestId("markdown-image-zoom")).toBeNull();
  });
});
