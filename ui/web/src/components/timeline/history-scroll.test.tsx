import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { StrictMode, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type TimelineResponse } from "@/lib/contracts/types";
import { readScrollMemory, saveScrollMemory } from "@/lib/layout/scroll-memory";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { resetTransport, transport } from "@/test-support/fake-api";
import { ApiProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { BAR_HEIGHT_PX } from "@/lib/layout/layout";
import { TimelineView } from ".";
import { TestStreamProvider } from "@/test-support/fake-stream";

beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
});
vi.mock("../content/markdown", () => ({ ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock("../ui/scroll-area", () => ({
  ScrollArea: ({
    children,
    ...rest
  }: {
    children: ReactNode;
    "data-activity-scroll"?: string;
  }) =>
    rest["data-activity-scroll"] === "true" ? (
      <div data-activity-scroll="true"><div>{children}</div></div>
    ) : (
      <div><div data-testid="history-viewport" data-slot="scroll-area-viewport">{children}</div></div>
    ),
}));
const items: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
  item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
  source: null, created_at: null, inbound_id: null, show_timestamp: false,
}));
const response: TimelineResponse = { items, msg_count: 17, has_more: false };
let top = 0;
let height = 5000;
let aboveSettle = 0;
let client: QueryClient;
let resizeCallbacks: Set<() => void>;
// meiva:note
// The timeline's config read is the only transport call this file needs; everything else
// must stay unanswered so an accidental read fails loudly.
const gateway = transport;

type Reveal = { itemId: string; token: number } | null;
function Conversation({ agentId, entry, reveal = null }: { agentId: number | null; entry: string; reveal?: Reveal }) {
  const timeline = useTimeline(agentId, () => undefined);
  return <TimelineView items={timeline.items} threadKey={agentId === null ? undefined : String(agentId)} scrollMemoryKey={entry} reveal={reveal} />;
}
const wallNow = () => Date.now();
function conversation(agentId: number | null, entry: string, reveal: Reveal = null) {
  return (
    <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}>
        <ApiProvider api={gateway}>
          <TestStreamProvider><Conversation agentId={agentId} entry={entry} reveal={reveal} key={entry} /></TestStreamProvider>
        </ApiProvider>
      </WallClockProvider>
    </QueryClientProvider>
  );
}
function viewport() { return screen.getByTestId("history-viewport"); }
function readingOffset(id: string) {
  const node = viewport().querySelector(`.timeline-item[data-item-id="${id}"]`);
  return node ? Math.round(node.getBoundingClientRect().top - viewport().getBoundingClientRect().top) : null;
}
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["timeline", 42], response);
  client.setQueryData(["timeline", 43], response);
  client.setQueryData(["config", null], { fields: [] });
  client.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS });
  useTimelineStore.getState().switchThread(null, null, false);
  top = 0;
  height = 5000;
  aboveSettle = 0;
  resizeCallbacks = new Set();
  vi.stubGlobal("ResizeObserver", class {
    private readonly deliver: () => void;
    constructor(callback: ResizeObserverCallback) {
      this.deliver = () => callback([], this);
    }
    observe(target: Element) {
      if (target.getAttribute("data-slot") === "scroll-area-viewport") resizeCallbacks.add(this.deliver);
    }
    disconnect() { resizeCallbacks.delete(this.deliver); }
    unobserve() { resizeCallbacks.delete(this.deliver); }
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    if (!this.querySelectorAll(".timeline-item").length) return 600;
    return height + [...this.querySelectorAll<HTMLElement>("[data-send-reserve], [data-turn-collapse-reserve]")]
      .reduce((total, node) => total + node.offsetHeight, 0);
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return Number.parseFloat(this.style.height) || 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation(function (this: HTMLElement, value) {
    top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const y = this.hasAttribute("data-send-reserve") ? height - top
      : this.dataset.itemId ? (Number.parseInt(this.dataset.itemId) - 1) * 300 - top + aboveSettle : 0;
    return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
  });
});
afterEach(() => {
  cleanup(); client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("history entry scroll ownership", () => {
  it("restores a returned entry while the real timeline hook seeds an empty live store", () => {
    saveScrollMemory("history-cold-live", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-cold-live"));
    expect(viewport().scrollTop).toBe(700);
  });
  it("restores a returned entry when live items are already measurable on its first commit", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-warm-live", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-warm-live"));
    expect(viewport().scrollTop).toBe(700);
  });
  it.each([false, true])("waits for a returned reader through partial layout (anchor=%s)", (anchored) => {
    height = 900;
    saveScrollMemory(`history-partial-layout-${anchored}`, { contentKey: "42", scrollTop: 700, followBottom: false, anchor: anchored ? { itemId: "1.0", rank: 0, viewportTop: -700 } : undefined });
    renderWithBrowser(conversation(42, `history-partial-layout-${anchored}`));
    resize();
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it("keeps the reading target through strict-mode partial layout", () => {
    height = 900;
    saveScrollMemory("history-partial-strict", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(<StrictMode>{conversation(42, "history-partial-strict")}</StrictMode>);
    resize();
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it.each([600, 900])("allows a selection to supersede a target at content height %i", (initialHeight) => {
    height = initialHeight;
    saveScrollMemory(`history-short-selection-${initialHeight}`, { contentKey: "42", scrollTop: 700, followBottom: false });
    const { rerender } = renderWithBrowser(conversation(42, `history-short-selection-${initialHeight}`));
    resize();
    rerender(conversation(43, `history-short-selection-${initialHeight}`));
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(4400);
  });
  it.each([600, 900])("a send whose message has not committed leaves a parked reader at their saved position at content height %i", (initialHeight) => {
    height = initialHeight;
    saveScrollMemory(`history-send-${initialHeight}`, { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, `history-send-${initialHeight}`));
    resize();
    act(() => { useTimelineStore.getState().requestSendAnchor(null); });
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it("keeps following when a restored reader scrolls back to the bottom and then sends", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-hold-release", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-hold-release"));
    resize();
    expect(viewport().scrollTop).toBe(700);
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    act(() => { useTimelineStore.getState().requestSendAnchor(null); });
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });
  it("a send releases a settled bookmark at the tail, so the stream pins again", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-tail-send", {
      contentKey: "42", scrollTop: 4400, followBottom: false,
      anchor: { itemId: "1.15", rank: 0, viewportTop: 0 },
    });
    renderWithBrowser(conversation(42, "history-tail-send"));
    expect(viewport().scrollTop).toBe(4400);
    act(() => { useTimelineStore.getState().requestSendAnchor(null); });
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });
  it("remembers a restored reader's later position across an agent switch", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-hold-save", { contentKey: "42", scrollTop: 700, followBottom: false });
    const { rerender } = renderWithBrowser(conversation(42, "history-hold-save"));
    resize();
    expect(viewport().scrollTop).toBe(700);
    act(() => { viewport().scrollTop = 1200; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-hold-save-other"));
    top = 0; height = 5000;
    rerender(conversation(42, "history-hold-save"));
    expect(viewport().scrollTop).toBe(1200);
  });
  it("lands on the end when a bookmark sits past a page that stopped growing", () => {
    height = 900;
    saveScrollMemory("history-unreachable", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-unreachable"));
    resize();
    resize();
    expect(viewport().scrollTop).toBe(300);
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(4400);
  });
  it("lets a reader replace an unreachable saved position by scrolling", () => {
    height = 900;
    saveScrollMemory("history-short-reader", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-short-reader"));
    resize();
    act(() => { viewport().scrollTop = 100; fireEvent.scroll(viewport()); });
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(100);
  });
  it("keeps the saved reader if the entry leaves before its layout can hold it", () => {
    height = 900;
    saveScrollMemory("history-short-leave", { contentKey: "42", scrollTop: 700, followBottom: false });
    const { rerender } = renderWithBrowser(conversation(42, "history-short-leave"));
    resize();
    act(() => { fireEvent.scroll(viewport()); });
    rerender(<QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>);
    top = 0; height = 5000;
    rerender(conversation(42, "history-short-leave"));
    expect(viewport().scrollTop).toBe(700);
  });
  it.each([-100, 0])("keeps the pending target when the controller absorbs a wheel at the bottom (delta=%s)", (deltaY) => {
    height = 900;
    saveScrollMemory(`history-short-wheel-${deltaY}`, { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, `history-short-wheel-${deltaY}`));
    resize();
    viewport().scrollTop = viewport().scrollHeight - viewport().clientHeight;
    act(() => { fireEvent.wheel(viewport(), { deltaY }); });
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it("keeps a restored reader parked when content grows", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-growth", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(conversation(42, "history-growth"));
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });
  it.each(["fresh-entry", "returning-follower", "mismatched-content"])("keeps following after %s", (entry) => {
    if (entry !== "fresh-entry") saveScrollMemory(entry, {
      contentKey: entry === "mismatched-content" ? "43" : "42",
      scrollTop: entry === "returning-follower" ? 4400 : 700,
      followBottom: entry === "returning-follower",
    });
    renderWithBrowser(conversation(42, entry));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });
  it("returns a saved follower to messages that arrived while away", () => {
    height = 6000;
    saveScrollMemory("history-away-growth", { contentKey: "42", scrollTop: 4400, followBottom: true });
    renderWithBrowser(conversation(42, "history-away-growth"));
    resize();
    expect(viewport().scrollTop).toBe(5400);
  });
  it("restores through a strict-mode effect re-attach", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-strict", { contentKey: "42", scrollTop: 700, followBottom: false });
    renderWithBrowser(<StrictMode>{conversation(42, "history-strict")}</StrictMode>);
    expect(viewport().scrollTop).toBe(700);
  });
  it("remembers independent positions across entry unmounts and returns", () => {
    const { rerender } = renderWithBrowser(conversation(42, "history-first"));
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-second"));
    act(() => { viewport().scrollTop = 1200; fireEvent.scroll(viewport()); });
    rerender(<QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>);
    top = 0;
    rerender(conversation(42, "history-first"));
    expect(viewport().scrollTop).toBe(700);
    rerender(<QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>);
    top = 0;
    rerender(conversation(43, "history-second"));
    expect(viewport().scrollTop).toBe(1200);
  });
  it("keeps a selection command after a restore", () => {
    saveScrollMemory("history-select-after-restore", { contentKey: "42", scrollTop: 700, followBottom: false });
    const { rerender } = renderWithBrowser(conversation(42, "history-select-after-restore"));
    expect(viewport().scrollTop).toBe(700);
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-select-after-restore"));
    expect(viewport().scrollTop).toBe(4400);
  });
});

function AgentTabs({ agentId, entry }: { agentId: number; entry: string }) {
  const timeline = useTimeline(agentId, () => undefined);
  return <TimelineView key={agentId} items={timeline.items} threadKey={String(agentId)}
    scrollMemoryKey={`${entry}:agent:${agentId}`} />;
}
function tabs(agentId: number, entry: string) {
  return (
    <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}>
        <ApiProvider api={gateway}>
          <TestStreamProvider><AgentTabs agentId={agentId} entry={entry} /></TestStreamProvider>
        </ApiProvider>
      </WallClockProvider>
    </QueryClientProvider>
  );
}

describe("agent switch reading positions", () => {
  it("restores independent A/B bookmarks inside the same page entry", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-tabs"));
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-tabs"));
    act(() => { viewport().scrollTop = 1200; fireEvent.scroll(viewport()); });
    rerender(tabs(42, "agent-tabs"));
    expect(viewport().scrollTop).toBe(700);
    rerender(tabs(43, "agent-tabs"));
    expect(viewport().scrollTop).toBe(1200);
  });
  it("restores a reader at the padded end without switching them to tail following", () => {
    const rows = [...items, { ...items[0], item_id: "17.0", kind: "inbound_chat", source: "user", payload: "Latest question" }];
    client.setQueryData(["timeline", 42], { ...response, items: rows });
    const { rerender } = renderWithBrowser(tabs(42, "agent-padded-bottom"));
    act(() => { viewport().scrollTop = 4744; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-padded-bottom:agent:42")).toMatchObject({ scrollTop: 4744, followBottom: false });
    rerender(tabs(43, "agent-padded-bottom"));
    rerender(tabs(42, "agent-padded-bottom"));
    resize();
    expect(viewport().scrollTop).toBe(4744);
    expect(readingOffset("17.0")).toBe(56);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("recreates the space needed for a bookmark beyond the shortened conversation", () => {
    saveScrollMemory("agent-collapse-space:agent:42", {
      contentKey: "42", scrollTop: 4600, followBottom: false, usedCollapseReserve: true,
      anchor: { itemId: "16.0", rank: 0, viewportTop: -100 },
    });
    const { rerender } = renderWithBrowser(tabs(43, "agent-collapse-space"));
    rerender(tabs(42, "agent-collapse-space"));
    resize();
    expect(viewport().scrollTop).toBe(4600);
    expect(readingOffset("16.0")).toBe(-100);
    expect(viewport().querySelector<HTMLElement>("[data-turn-collapse-reserve]")?.style.height).toBe("200px");
  });
  it("saves a reader on the tail as following the bottom", () => {
    renderWithBrowser(tabs(42, "agent-tail-save"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-tail-save:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4400, followBottom: true,
    });
  });
  it("returns a bottom follower to the newest message after messages arrived while away", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-tail"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-new-tail"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-tail"));
    expect(viewport().scrollTop).toBe(5400);
    resize();
    expect(viewport().scrollTop).toBe(5400);
    // meiva:note
    // A reader at the bottom has nothing to scroll to, so the control is not in
    // the DOM at all (user report 2026-10-30).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });
  it("keeps following a returned bottom follower as the thread grows", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-tail-growth"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-new-tail-growth"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-tail-growth"));
    resize();
    act(() => { height = 7000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(6400);
  });
  it("keeps following when the switch lands between a chunk and its pin", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-chunk-frame"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    act(() => { height = 6000; });
    rerender(tabs(43, "agent-chunk-frame"));
    expect(readScrollMemory("agent-chunk-frame:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4400, followBottom: true,
    });
    rerender(tabs(42, "agent-chunk-frame"));
    resize();
    expect(viewport().scrollTop).toBe(5400);
  });
  it("reads the wider touch bottom zone as the tail", () => {
    vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query === "(pointer: coarse)", media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    renderWithBrowser(tabs(42, "agent-touch-zone"));
    act(() => { viewport().scrollTop = 4300; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-touch-zone:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4300, followBottom: true,
    });
  });
  it("returns a parked reader to their item after messages arrived while away", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-parked"));
    act(() => { viewport().scrollTop = 4300; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-new-parked:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4300, followBottom: false,
      anchor: { itemId: "15.0", rank: 0, viewportTop: -100 },
    });
    rerender(tabs(43, "agent-new-parked"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-parked"));
    resize();
    expect(viewport().scrollTop).toBe(4300);
    expect(readingOffset("15.0")).toBe(-100);
  });
  it("retains scroll-loaded history and keeps it after a newer tail snapshot", () => {
    const older: BackendTimelineItem = { ...items[0], item_id: "s1.old.1.0", payload: "Previously loaded history" };
    const { rerender } = renderWithBrowser(tabs(42, "agent-old-window"));
    act(() => { useTimelineStore.getState().prependOlder([older], true); });
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-old-window"));
    expect(screen.queryByText("Previously loaded history")).toBeNull();
    rerender(tabs(42, "agent-old-window"));
    expect(screen.getByText("Previously loaded history")).toBeTruthy();
    expect(viewport().scrollTop).toBe(700);
    act(() => useTimelineStore.getState().reloadSnapshot([...items, { ...items[0], item_id: "17.0", payload: "New reply" }], 18, false));
    resize();
    expect(screen.getByText("Previously loaded history")).toBeTruthy();
    expect(viewport().scrollTop).toBe(700);
  });
  it("keeps the down arrow visible during an older-history load", () => {
    renderWithBrowser(<TimelineView items={items} threadKey="one-ring" loadingOlder readerLoadingOlder />, {
      wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    });
    act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
    expect(screen.getByRole("button", { name: "Scroll down" }).querySelector(".animate-spin")).toBeNull();
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("loading");
  });
});

describe("restored position settles", () => {
  it("keeps a restored reading item at its saved offset while the rows above settle", () => {
    const entry = "history-settle";
    saveScrollMemory(entry, { contentKey: "42", scrollTop: 700, followBottom: false, anchor: { itemId: "3.0", rank: 0, viewportTop: -100 } });
    renderWithBrowser(conversation(42, entry));
    expect(viewport().scrollTop).toBe(700);
    act(() => { aboveSettle = -50; resize(); });
    expect(viewport().scrollTop).toBe(650);
    expect(readingOffset("3.0")).toBe(-100);
    act(() => { aboveSettle = -80; resize(); });
    expect(viewport().scrollTop).toBe(620);
    expect(readingOffset("3.0")).toBe(-100);
  });
  it("keeps the saved bookmark while the restored window is still settling", () => {
    const entry = "history-settle-bookmark";
    saveScrollMemory(entry, { contentKey: "42", scrollTop: 700, followBottom: false, anchor: { itemId: "3.0", rank: 0, viewportTop: -100 } });
    renderWithBrowser(conversation(42, entry));
    act(() => { aboveSettle = -50; resize(); });
    act(() => { fireEvent.scroll(viewport()); });
    expect(readScrollMemory(entry)).toEqual({ contentKey: "42", scrollTop: 700, followBottom: false, anchor: { itemId: "3.0", rank: 0, viewportTop: -100 } });
    act(() => { resize(); });
    act(() => { fireEvent.scroll(viewport()); });
    expect(readScrollMemory(entry)).toEqual({ contentKey: "42", scrollTop: 650, followBottom: false, anchor: { itemId: "3.0", rank: 0, viewportTop: -100 } });
  });
  it("keeps the bookmark through a switch away inside the settle", () => {
    const entry = "history-settle-switch";
    saveScrollMemory(entry, { contentKey: "42", scrollTop: 700, followBottom: false, anchor: { itemId: "3.0", rank: 0, viewportTop: -100 } });
    const { rerender } = renderWithBrowser(conversation(42, entry));
    act(() => { aboveSettle = -50; resize(); });
    act(() => { fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-settle-other"));
    rerender(conversation(42, entry));
    expect(viewport().scrollTop).toBe(650);
    expect(readingOffset("3.0")).toBe(-100);
  });
});
it("captures the departing reading position before the native pull reserve is removed", () => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const reserve = Number.parseFloat(this.closest('[data-slot="scroll-area-viewport"]')?.querySelector<HTMLElement>('[data-history-pull-space]')?.style.height ?? "0") || 0;
    const y = this.dataset.itemId ? reserve + (Number.parseInt(this.dataset.itemId) - 1) * 300 - top : 0;
    return { top: y, bottom: y + 300, height: 300, left: 0, right: 400, width: 400 } as DOMRect;
  });
  const { rerender } = renderWithBrowser(tabs(42, "agent-native-reserve"));
  act(() => { viewport().scrollTop = 700; fireEvent.scroll(viewport()); });
  rerender(tabs(43, "agent-native-reserve"));
  rerender(tabs(42, "agent-native-reserve"));
  expect(viewport().scrollTop).toBe(700);
});
  it("keeps the reader on their row when the viewport width changes", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    renderWithBrowser(conversation(42, "history-width-change"));
    resize();
    act(() => { viewport().scrollTop = 900; fireEvent.scroll(viewport()); });
    expect(readingOffset("4.0")).toBe(0);
    let width = 400;
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
    // meiva:note
    // The first pass only records the width the viewport already had.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    act(() => { resizeCallbacks.forEach((deliver) => deliver()); });

    // meiva:note
    // A width change re-wraps every row; the ones above the reader grew, so the
    // pixel offset that used to point at 4.0 now points past it.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    act(() => {
      width = 700;
      aboveSettle = 120;
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(readingOffset("4.0")).toBe(0);
    expect(viewport().scrollTop).toBe(1020);
  });

// meiva:note
// A reveal request rides the bookmark restore: the item lands in the upper part of the viewport, a
// new token is a new request, and a request naming no loaded item changes nothing.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
describe("revealing an item", () => {
  const upperPart = BAR_HEIGHT_PX + 12;
  it("brings the item to the upper part of the viewport instead of the bottom", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, "reveal-entry"));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    rerender(conversation(42, "reveal-entry", { itemId: "8.0", token: 1 }));
    resize();
    expect(readingOffset("8.0")).toBe(upperPart);
    expect(viewport().scrollTop).toBe(7 * 300 - upperPart);
  });
  it("treats each token as a new request, so a reader who scrolled away is brought back", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, "reveal-again"));
    rerender(conversation(42, "reveal-again", { itemId: "8.0", token: 1 }));
    resize();
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    rerender(conversation(42, "reveal-again", { itemId: "8.0", token: 2 }));
    resize();
    expect(readingOffset("8.0")).toBe(upperPart);
  });
  it("leaves the reader where they are when the item is not loaded", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, "reveal-missing"));
    resize();
    rerender(conversation(42, "reveal-missing", { itemId: "99.0", token: 1 }));
    resize();
    expect(viewport().scrollTop).toBe(4400);
  });
});
