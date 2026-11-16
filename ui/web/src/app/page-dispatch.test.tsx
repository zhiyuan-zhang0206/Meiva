import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupHomePageTest, gatewayWrapper, HomePageView, homePageProps, HomePageBound as HomePage, hooksState, makeAgent, resetHomePageTestState, wrap } from "./page-test-mock";

import { resetTransport, transport } from "@/test-support/fake-api";
import { TimelineView } from "@/components/timeline";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type TimelineResponse } from "@/lib/contracts/types";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { fakeStreamHub, TestStreamProvider } from "@/test-support/fake-stream";

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
      expect(transport.sendMessage).toHaveBeenCalledWith(5, "hi", expect.any(String)),
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
  client.setQueryData(["user-settings"], hooksState.settings);
  client.setQueryData(["config", null], { fields: [] });
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(5000);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation((value) => { top = Math.max(0, Math.min(4400, value)); });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const y = this.dataset.itemId ? (Number.parseInt(this.dataset.itemId) - 1) * 300 - top : 0;
    return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
  });
});
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks(); });

  it.each(["reader", "background"])("presents %s reads through the home consumer and shows a reader retry after failure", async (origin) => {
    let fail: (reason: Error) => void = () => undefined;
    transport.getTimeline.mockImplementation(() => new Promise<TimelineResponse>((_, reject) => { fail = reject; }));
    renderWithBrowser(<QueryClientProvider client={client}>
      <ApiProvider api={transport}><StreamHubProvider hub={hub}><WallClockProvider wallNow={Date.now}>
        <TestStreamProvider><HomeReader /></TestStreamProvider>
      </WallClockProvider></StreamHubProvider></ApiProvider>
    </QueryClientProvider>);
    const viewport = document.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
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
