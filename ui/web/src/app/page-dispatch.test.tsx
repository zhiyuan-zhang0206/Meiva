import { useMemo, useRef, type ComponentProps } from "react";
import { timelineGeometry } from "@/test-support/timeline-geometry";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupHomePageTest, gatewayWrapper, HomePageView, homePageProps, HomePageBound as HomePage, hooksState, makeAgent, resetHomePageTestState, wrap } from "./page-test-mock";

import { resetTransport, transport } from "@/test-support/fake-api";
import { hasSavedTimelinePlace, TimelineView } from "@/components/timeline";
import { fakeBrowser } from "@/lib/browser/testing";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type TimelineResponse, type ConversationSnapshotResponse, type UserMessageFeed } from "@/lib/contracts/types";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { fakeStreamHub, TestStreamProvider } from "@/test-support/fake-stream";
import { deferred } from "@/test-support/deferred";
import { httpRig, noUpload, sequentialKeys } from "@/lib/transport/tests/api/api-provider";

function composed(timeline: TimelineResponse): ConversationSnapshotResponse {
  return { timeline, pending: [], token_usage: { input_tokens: 0, output_tokens: 0,
    reasoning_tokens: 0, max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0 } };
}

const NO_SESSION = { state: "disabled" } as const;
const timelineViewport = () => document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
// meiva:note
// Rows are mounted but hidden until the timeline has placed them.
const rowsShown = () => timelineViewport().querySelector<HTMLElement>("[data-timeline-column]")?.style.visibility !== "hidden";

// meiva:note
// Every frame the reader could see: after each batch of DOM changes (the commit and the layout
// effects that follow it, before the browser would paint), the scroll offset whenever rows are shown.
function recordShownFrames() {
  const tops: number[] = [];
  const sample = () => {
    const viewport = document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (viewport?.querySelector(".timeline-item") && rowsShown()) tops.push(viewport.scrollTop);
  };
  const observer = new MutationObserver(sample);
  observer.observe(document.body, { subtree: true, childList: true, attributes: true });
  return { tops, sample, stop: () => observer.disconnect() };
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

  let filledTop = 0;
  const filled = timelineGeometry({ top: () => filledTop, setTop: (value) => { filledTop = value; }, height: () => 5000, resizeCallbacks: new Set() });
  // meiva:note
  // A viewport the conversation fills: a short first page would start the cold fill's own older read.
  function FilledTimeline(props: ComponentProps<typeof TimelineView>) {
    return <div {...filled.root}><TimelineView {...props} viewportObserverFactory={filled.observer} /></div>;
  }

  function Reader({ id = 42, running = true, unread = false, fills = false }: { id?: number; running?: boolean; unread?: boolean; fills?: boolean }) {
    const timeline = useTimeline(id, () => undefined, running ? "running" : "idling");
    const props = homePageProps();
    const agent = makeAgent({ agent_id: id, status: running ? "running" : "idling", has_unread_user_messages: unread });
    return <HomePageView {...props} agents={{ ...props.agents, activeId: id, activeAgent: agent, agents: [agent] }}
      timeline={timeline} TimelineView={fills ? FilledTimeline : TimelineView} bfcacheId={`home-loading-${visit}`} />;
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
    filledTop = 0;
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
    const view = renderWithBrowser(surface(<Reader fills />));
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(transport.getTimeline).not.toHaveBeenCalled();
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

  it.each([
    { unread: false, opens: "once the timeline read lands, without waiting for the seen feed" },
    { unread: true, opens: "only once the seen feed has decided where it opens" },
  ])("a first visit the roster marks unread=$unread shows its rows $opens", async ({ unread }) => {
    const feed = deferred<UserMessageFeed>();
    transport.getUserMessages.mockReturnValue(feed.promise);
    const read = deferred<TimelineResponse>();
    transport.getTimeline.mockReturnValue(read.promise);
    renderWithBrowser(surface(<Reader running={false} unread={unread} />));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    await act(() => { read.resolve(response([human])); return read.promise; });
    await waitFor(() => expect(screen.getByText("Selected prompt")).toBeTruthy());
    expect(transport.getUserMessages).toHaveBeenCalledWith(42, expect.anything());
    if (unread) {
      expect(rowsShown()).toBe(false);
      expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
      await act(() => { feed.resolve({ mode: "prefix", seen_id: 0, items: [] }); return feed.promise; });
    }
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(rowsShown()).toBe(true);
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
const geometry = timelineGeometry({ top: () => top, setTop: (value) => { top = value; }, height: () => 5000, resizeCallbacks: new Set() });

function MeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  return <div {...geometry.root}><TimelineView {...props} viewportObserverFactory={geometry.observer} /></div>;
}

function HomeReader() {
  const timeline = useTimeline(42, () => undefined);
  return <><button type="button" onClick={() => timeline.loadOlder("background")}>Background fixture read</button>
    <HomePageView {...homePageProps()} timeline={timeline} TimelineView={MeasuredTimeline} /></>;
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
    const viewport = timelineViewport();
    expect(viewport.scrollTop).toBe(4400);
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
let freshHeight = 5000;
let geometry: ReturnType<typeof timelineGeometry>;
let resizeCallbacks: Set<() => void>;
const viewport = timelineViewport;
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }
function readingOffset(id: string) {
  const node = viewport().querySelector(`.timeline-item[data-item-id="${id}"]`);
  return node ? Math.round(node.getBoundingClientRect().top - viewport().getBoundingClientRect().top) : null;
}
function seedCaches() {
  client.setQueryData(["timeline", 42], response);
  client.setQueryData(["timeline", 43], response);
  client.setQueryData(["config", null], { fields: [] });
  client.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS });
}
beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  seedCaches();
  transport.getConversationSnapshot.mockResolvedValue(composed(response));
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
  freshHeight = 5000;
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
  return <div {...geometry.root}><TimelineView {...props} viewportObserverFactory={geometry.observer} /></div>;
}

// meiva:note
// A viewport of its own per mount: a remounted timeline starts at scrollTop 0, as in a browser.
function FreshMeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  const ownTop = useRef(0);
  const measured = useMemo(() => timelineGeometry({
    top: () => ownTop.current, setTop: (value) => { ownTop.current = value; },
    height: () => freshHeight, resizeCallbacks,
  }), []);
  return <div {...measured.root}><TimelineView {...props} viewportObserverFactory={measured.observer} /></div>;
}

  it.each(["cold", "retained"])("returns a departed reader to their row, showing no other position, through a %s source read", async (source) => {
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
    expect(readingOffset("3.0")).toBe(-100);
    hooksState.activeId = 43;
    view.rerender(home());
    expect(hasSavedTimelinePlace(`home-loading-return-${source}:agent:42`, "42", NO_SESSION)).toBe(true);
    if (source === "cold") act(() => {
      client.removeQueries({ queryKey: ["timeline", 42], exact: true });
      client.removeQueries({ queryKey: ["timeline-reading-window", 42], exact: true });
    });
    let deliver: (value: TimelineResponse) => void = () => undefined;
    transport.getTimeline.mockImplementation(() => new Promise<TimelineResponse>((resolve) => { deliver = resolve; }));
    top = 0;
    const frames = recordShownFrames();
    hooksState.activeId = 42;
    view.rerender(home());
    frames.sample();
    if (source === "retained") {
      // meiva:note
      // The retained rows hold the saved row: the first layout already stands on it.
      expect(rowsShown()).toBe(true);
      expect(readingOffset("3.0")).toBe(-100);
    } else {
      expect(rowsShown()).toBe(false);
      await waitFor(() => expect(transport.getTimeline).toHaveBeenCalled());
      resize(); resize(); resize();
      expect(rowsShown()).toBe(false);
      await act(async () => { deliver(response); await Promise.resolve(); });
    }
    await waitFor(() => expect(viewport().querySelectorAll(".timeline-item")).toHaveLength(16));
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    resize();
    frames.sample();
    frames.stop();
    expect(viewport().scrollTop).toBe(700);
    expect(readingOffset("3.0")).toBe(-100);
    expect(new Set(frames.tops)).toEqual(new Set([700]));
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
    let current: ReturnType<typeof useTimeline> | undefined;
    const { hub } = fakeStreamHub();
    function HomeReader() {
      const timeline = useTimeline(42, () => undefined);
      current = timeline;
      return <HomePageView {...homePageProps()} timeline={timeline} TimelineView={MeasuredTimeline} />;
    }
    const home = () => <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}><ApiProvider api={gateway}>
        <StreamHubProvider hub={hub}><TestStreamProvider><HomeReader /></TestStreamProvider></StreamHubProvider>
      </ApiProvider></WallClockProvider>
    </QueryClientProvider>;
    if (outcome !== "no-saved") {
      // meiva:note
      // An earlier visit in this page entry left the reader 100px into row 3.0.
      const visit = renderWithBrowser(home());
      await waitFor(() => expect(current?.sourceReady).toBe(true));
      act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
      visit.unmount();
      expect(hasSavedTimelinePlace(entryKey, "42", NO_SESSION)).toBe(true);
      client.clear();
      seedCaches();
      useTimelineStore.getState().switchThread(null, null, false);
      transport.getConversationSnapshot.mockClear();
      top = 0;
    }
    let deliver: (value: ConversationSnapshotResponse) => void = () => undefined;
    let fail: (reason: Error) => void = () => undefined;
    transport.getConversationSnapshot.mockImplementation(() => new Promise<ConversationSnapshotResponse>((resolve, reject) => { deliver = resolve; fail = reject; }));
    transport.getTimeline.mockRejectedValue(new Error("fixture authority unavailable"));
    const frames = recordShownFrames();
    renderWithBrowser(home());
    frames.sample();
    expect(current?.isLoading).toBe(true);
    // meiva:note
    // The first layout stands where this entry opens while its authority read is pending: the saved
    // row for a return, the tail for a first visit.
    const opening = outcome === "no-saved" ? 4400 : 700;
    expect(rowsShown()).toBe(true);
    expect(top).toBe(opening);
    resize(); resize(); resize();
    expect(top).toBe(opening);
    if (outcome !== "no-saved") expect(readingOffset("3.0")).toBe(-100);
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
      // meiva:note
      // A failed authority read keeps the reader's place.
      expect(top).toBe(700);
      expect(readingOffset("3.0")).toBe(-100);
      expect(hasSavedTimelinePlace(entryKey, "42", NO_SESSION)).toBe(true);
      transport.getTimeline.mockResolvedValue(response);
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    } else act(() => { deliver(composed(fresh)); });
    await waitFor(() => expect(current?.isLoading).toBe(false));
    resize();
    frames.sample();
    frames.stop();
    if (outcome === "unchanged" || outcome === "error") {
      expect(top).toBe(700);
      expect(readingOffset("3.0")).toBe(-100);
      expect(new Set(frames.tops)).toEqual(new Set([700]));
    }
    if (outcome === "no-saved") expect(new Set(frames.tops)).toEqual(new Set([4400]));
    if (outcome === "new") expect(current?.items.some((item) => item.payload === "Fresh 0")).toBe(true);
    const background = new Promise<TimelineResponse>(() => undefined);
    transport.getTimeline.mockReturnValue(background);
    act(() => { void client.invalidateQueries({ queryKey: ["timeline", 42] }); });
    await waitFor(() => expect(current?.isFetching).toBe(true));
    expect(current?.isLoading).toBe(false);
  });

  it("keeps a reader who left at the tail on the row they left, through the seen advance and newer output", async () => {
    const authority = deferred<ConversationSnapshotResponse>();
    const empty: TimelineResponse = { items: [], msg_count: 0, has_more: false };
    const newer: BackendTimelineItem = { ...items[0], item_id: "17.0", payload: "Newer output" };
    let holdAuthority = false;
    let unread = false;
    let seen = 0;
    let current: ReturnType<typeof useTimeline> | undefined;
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
      const agent = makeAgent({ agent_id: id, status: "idling", has_unread_user_messages: unread });
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
    expect(atBottom()).toBe(true);
    expect(readingOffset("15.0")).toBe(-200);
    view.rerender(surface(43));
    await waitFor(() => expect(current?.sourceReady).toBe(true));
    expect(hasSavedTimelinePlace(`${entry}:agent:42`, "42", NO_SESSION)).toBe(true);
    // meiva:note
    // A delivery arrived while the reader was away; the authority read of the return is held.
    unread = true; holdAuthority = true;
    const frames = recordShownFrames();
    view.rerender(surface(42));
    frames.sample();
    expect(rowsShown()).toBe(true);
    expect(readingOffset("15.0")).toBe(-200);
    await waitFor(() => expect(client.getQueryData<{ seen_id: number }>(["user-messages", 42])?.seen_id).toBe(1));
    expect(current?.sourceReady).toBe(false);
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(readingOffset("15.0")).toBe(-200);
    act(() => {
      freshHeight = 5300;
      authority.resolve(composed({ items: [...items, newer], msg_count: 18, has_more: false }));
    });
    await waitFor(() => expect(current?.sourceReady).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("cold-load-ring")).toBeNull());
    expect(screen.getByText("Newer output")).toBeTruthy();
    resize();
    frames.sample();
    frames.stop();
    expect(readingOffset("15.0")).toBe(-200);
    expect(atBottom()).toBe(false);
    expect(new Set(frames.tops)).toEqual(new Set([4400]));
  });

});

describe("feature.reloadReadingRestore on HomePageView", () => {
  it("short-circuits all Browser.session reading-bookmark reads and writes when feature.reloadReadingRestore is false", () => {
    let localTop = 0;
    const localCallbacks = new Set<() => void>();
    const localGeometry = timelineGeometry({
      top: () => localTop,
      setTop: (value) => {
        localTop = value;
      },
      height: () => 5000,
      resizeCallbacks: localCallbacks,
    });
    function LocalMeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
      return (
        <div {...localGeometry.root}>
          <TimelineView {...props} viewportObserverFactory={localGeometry.observer} />
        </div>
      );
    }
    const localViewport = timelineViewport;
    const localResize = () => {
      act(() => {
        localCallbacks.forEach((deliver) => deliver());
      });
    };
    const localReadingOffset = (id: string) => {
      const node = localViewport().querySelector(`.timeline-item[data-item-id="${id}"]`);
      return node
        ? Math.round(node.getBoundingClientRect().top - localViewport().getBoundingClientRect().top)
        : null;
    };

    const baseBrowser = fakeBrowser({ session: {} });
    const bookmarkReads: string[] = [];
    const bookmarkWrites: string[] = [];
    const BOOKMARKS_KEY = "meiva.timeline.reading-bookmarks.v1";
    baseBrowser.session.write(
      BOOKMARKS_KEY,
      JSON.stringify({
        "42": {
          readingLocator: "loc-3",
          itemId: "3.0",
          rank: 0,
          viewportTop: -100,
          scrollTop: 700,
          savedAt: 1000,
        },
      }),
    );
    const browser = {
      ...baseBrowser,
      session: {
        read(key: string) {
          if (key === BOOKMARKS_KEY) bookmarkReads.push(key);
          return baseBrowser.session.read(key);
        },
        write(key: string, value: string) {
          if (key === BOOKMARKS_KEY) bookmarkWrites.push(key);
          return baseBrowser.session.write(key, value);
        },
        remove(key: string) {
          if (key === BOOKMARKS_KEY) bookmarkWrites.push(`remove:${key}`);
          return baseBrowser.session.remove(key);
        },
      },
    };

    const agent = makeAgent({ agent_id: 42, status: "idling" });
    const locatedItems: BackendTimelineItem[] = Array.from({ length: 16 }, (_, idx) => ({
      item_id: `${idx + 1}.0`,
      kind: "agent_chat",
      payload: `Reply ${idx + 1}`,
      source: null,
      created_at: null,
      inbound_id: null,
      show_timestamp: false,
      reading_locator: `loc-${idx + 1}`,
    }));
    transport.getPendingMessages.mockResolvedValue([]);
    transport.getAgentQuestions.mockResolvedValue([]);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const renderHome = (enabled: boolean, settingsLoading: boolean, bfcacheId: string) => {
      const props = homePageProps();
      return (
        <WallClockProvider wallNow={() => 2000}>
          <QueryClientProvider client={qc}>
            <HomePageView
              {...props}
              bfcacheId={bfcacheId}
              agents={{ ...props.agents, activeId: 42, activeAgent: agent, agents: [agent] }}
              settings={{
                settings: {
                  ...USER_SETTING_DEFAULTS,
                  "behavior.replySuggestions": false,
                  "feature.reloadReadingRestore": enabled,
                },
                isLoading: settingsLoading,
              }}
              timeline={{ ...props.timeline, items: locatedItems, sourceReady: true, isLoading: false }}
              TimelineView={LocalMeasuredTimeline}
            />
          </QueryClientProvider>
        </WallClockProvider>
      );
    };

    // meiva:note
    // Cold reload where settings start loading=true, then resolve with feature.reloadReadingRestore=false.
    const disabledView = renderWithBrowser(renderHome(false, true, "reload-disabled-1"), {
      browser,
      wrapper: gatewayWrapper,
    });
    expect(bookmarkReads).toEqual([]);
    expect(bookmarkWrites).toEqual([]);

    disabledView.rerender(renderHome(false, false, "reload-disabled-1"));
    localResize();
    expect(localViewport().scrollTop).toBe(4400);
    act(() => {
      localViewport().scrollTop = 900;
      fireEvent.scroll(localViewport());
    });
    expect(localReadingOffset("4.0")).toBe(0);
    disabledView.unmount();
    expect(bookmarkReads).toEqual([]);
    expect(bookmarkWrites).toEqual([]);

    // meiva:note
    // The page's own memory still returns the reader with the reload bookmark disabled.
    localTop = 0;
    const returnView = renderWithBrowser(renderHome(false, false, "reload-disabled-1"), {
      browser,
      wrapper: gatewayWrapper,
    });
    expect(localViewport().scrollTop).toBe(900);
    expect(localReadingOffset("4.0")).toBe(0);
    returnView.unmount();
    expect(bookmarkReads).toEqual([]);
    expect(bookmarkWrites).toEqual([]);

    // meiva:note
    // Enabled reload reads the stored session bookmark and opens on its row and offset.
    localTop = 0;
    const frames = recordShownFrames();
    renderWithBrowser(renderHome(true, false, "reload-enabled-2"), {
      browser,
      wrapper: gatewayWrapper,
    });
    frames.sample();
    expect(bookmarkReads.length).toBeGreaterThan(0);
    expect(localViewport().scrollTop).toBe(700);
    expect(localReadingOffset("3.0")).toBe(-100);
    localResize();
    frames.sample();
    frames.stop();
    expect(localViewport().scrollTop).toBe(700);
    expect(new Set(frames.tops)).toEqual(new Set([700]));
  });
});
