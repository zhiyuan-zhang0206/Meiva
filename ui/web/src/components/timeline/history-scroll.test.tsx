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
import { fakeBrowser } from "@/lib/browser/testing";
import { TimelineView } from ".";
import {
  matchReloadBookmark,
  oldestReloadCursorId,
  readReloadBookmark,
  RELOAD_BOOKMARK_MAX_AGENTS,
  RELOAD_BOOKMARK_TTL_MS,
  RELOAD_BOOKMARKS_STORAGE_KEY,
  saveReloadBookmark,
} from "./model/reading-position";
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
  it("a send keeps a returned reading bookmark at the tail in place", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    saveScrollMemory("history-tail-send", {
      contentKey: "42", scrollTop: 4400, followBottom: false,
      anchor: { itemId: "1.15", rank: 0, viewportTop: 0 },
    });
    renderWithBrowser(conversation(42, "history-tail-send"));
    expect(viewport().scrollTop).toBe(4400);
    act(() => { useTimelineStore.getState().requestSendAnchor(null); });
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(4400);
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
    expect(viewport().scrollTop).toBe(300);
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
  it("honors an explicitly supplied follow-bottom bookmark after growth", () => {
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

function AgentTabs({ agentId, entry, turnActive }: { agentId: number; entry: string; turnActive: boolean }) {
  const timeline = useTimeline(agentId, () => undefined);
  return <TimelineView key={agentId} items={timeline.items} threadKey={String(agentId)}
    scrollMemoryKey={`${entry}:agent:${agentId}`} turnActive={turnActive} />;
}
function tabs(agentId: number, entry: string, turnActive = false) {
  return (
    <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}>
        <ApiProvider api={gateway}>
          <TestStreamProvider><AgentTabs agentId={agentId} entry={entry} turnActive={turnActive} /></TestStreamProvider>
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
    const { rerender } = renderWithBrowser(tabs(42, "agent-padded-bottom", true));
    act(() => { viewport().scrollTop = 4744; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-padded-bottom:agent:42")).toMatchObject({ scrollTop: 4744, followBottom: false });
    rerender(tabs(42, "agent-padded-bottom"));
    rerender(tabs(43, "agent-padded-bottom"));
    rerender(tabs(42, "agent-padded-bottom"));
    resize();
    expect(viewport().scrollTop).toBe(4744);
    expect(readingOffset("17.0")).toBe(56);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it.each([false, true])("preserves the departure bookmark and reserve availability across active return (keep: %s)", (keep) => {
    const entry = `combined-reserve-${keep}`;
    const rows = [...items, { ...items[0], item_id: "17.0", kind: "inbound_chat", source: "user", payload: "Question" }];
    client.setQueryData(["timeline", 42], { ...response, items: rows });
    const { rerender } = renderWithBrowser(tabs(42, entry, true));
    act(() => { viewport().scrollTop = 4744; fireEvent.scroll(viewport()); });
    if (!keep) act(() => {
      fireEvent.wheel(viewport(), { deltaY: -344 });
      viewport().scrollTop = 4400;
      fireEvent.scroll(viewport());
    });
    const expected = top;
    rerender(tabs(43, entry));
    act(() => { height = 6000; });
    rerender(tabs(42, entry, true));
    resize();
    expect(top).toBe(expected);
    expect(readScrollMemory(`${entry}:agent:42`)?.keepSendReserve === true).toBe(keep);
    expect(readingOffset(keep ? "17.0" : "15.0")).toBe(keep ? 56 : -200);
    act(() => { height = 7000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(top).toBe(expected);
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
  it("saves the visible item and offset even at the tail", () => {
    renderWithBrowser(tabs(42, "agent-tail-save"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-tail-save:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4400, followBottom: false,
      anchor: { itemId: "15.0", rank: 0, viewportTop: -200 },
    });
  });
  it("returns a former bottom follower to the same item after messages arrived while away", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-tail"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-new-tail"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-tail"));
    expect(viewport().scrollTop).toBe(4400);
    resize();
    expect(viewport().scrollTop).toBe(4400);
    expect(readingOffset("15.0")).toBe(-200);
    expect(screen.queryByTestId("scroll-to-bottom")).not.toBeNull();
  });
  it("returns without following even when no content arrived while away", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-tail-no-growth"));
    rerender(tabs(43, "agent-tail-no-growth"));
    rerender(tabs(42, "agent-tail-no-growth"));
    resize();
    expect(top).toBe(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(top).toBe(4400);
    expect(readingOffset("15.0")).toBe(-200);
  });
  it("holds a returned bottom bookmark as the thread grows", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-tail-growth"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    rerender(tabs(43, "agent-new-tail-growth"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-tail-growth"));
    resize();
    act(() => { height = 7000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(4400);
  });
  it("keeps the visible bookmark when the switch lands between a chunk and its pin", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-chunk-frame"));
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    act(() => { height = 6000; });
    rerender(tabs(43, "agent-chunk-frame"));
    expect(readScrollMemory("agent-chunk-frame:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4400, followBottom: false,
      anchor: { itemId: "15.0", rank: 0, viewportTop: -200 },
    });
    rerender(tabs(42, "agent-chunk-frame"));
    resize();
    expect(viewport().scrollTop).toBe(4400);
  });
  it("bookmarks the visible row inside the wider touch bottom zone", () => {
    vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query === "(pointer: coarse)", media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    renderWithBrowser(tabs(42, "agent-touch-zone"));
    act(() => { viewport().scrollTop = 4300; fireEvent.scroll(viewport()); });
    expect(readScrollMemory("agent-touch-zone:agent:42")).toEqual({
      contentKey: "42", scrollTop: 4300, followBottom: false,
      anchor: { itemId: "15.0", rank: 0, viewportTop: -100 },
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
    act(() => { useTimelineStore.getState().reloadSnapshot([...items, { ...items[0], item_id: "17.0", payload: "New reply" }], 18, false); });
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
  it.each(["load", "error"])("keeps a parked reader on their row after image %s in short history", async (event) => {
    const withImage = items.map<BackendTimelineItem>((item, index) => index === 0
      ? { ...item, kind: "inbound_chat", source: "user", images: ["/picture.png"] } : item);
    client.setQueryData(["timeline", 42], { ...response, items: withImage });
    useTimelineStore.getState().switchThread(42, withImage, false);
    renderWithBrowser(conversation(42, `history-media-${event}`));
    resize();
    act(() => { viewport().scrollTop = 900; fireEvent.scroll(viewport()); });
    expect(readingOffset("4.0")).toBe(0);
    act(() => {
      aboveSettle = event === "load" ? 120 : -20;
      fireEvent(screen.getByAltText("attached image"), new Event(event));
    });
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(readingOffset("4.0")).toBe(0);
    expect(viewport().scrollTop).toBe(event === "load" ? 1020 : 880);
  });
  it("uses the reader's latest row when navigation precedes a queued image decode", async () => {
    const withImage = items.map<BackendTimelineItem>((item, index) => index === 0
      ? { ...item, kind: "inbound_chat", source: "user", images: ["/picture.png"] } : item);
    client.setQueryData(["timeline", 42], { ...response, items: withImage });
    renderWithBrowser(conversation(42, "history-media-navigation"));
    resize();
    act(() => { viewport().scrollTop = 900; fireEvent.scroll(viewport()); });
    act(() => {
      fireEvent.load(screen.getByAltText("attached image"));
      viewport().scrollTop = 1200;
      fireEvent.scroll(viewport());
      aboveSettle = 120;
    });
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(readingOffset("5.0")).toBe(0);
    expect(viewport().scrollTop).toBe(1320);
    act(() => { aboveSettle = 240; fireEvent.load(screen.getByAltText("attached image")); });
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(readingOffset("5.0")).toBe(0);
    act(() => { viewport().scrollTop = 4400; fireEvent.scroll(viewport()); });
    act(() => { aboveSettle = 360; fireEvent.load(screen.getByAltText("attached image")); });
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(viewport().scrollTop).toBe(4400);
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
  it("stays at the bottom when an unseen delivery is revealed after switching back", () => {
    const entry = "reveal-returning-follower";
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, entry));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    rerender(conversation(43, "reveal-other-thread"));
    rerender(conversation(42, entry));
    resize();
    expect(viewport().scrollTop).toBe(4400);

    rerender(conversation(42, entry, { itemId: "1.0", token: 1 }));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });
  it("keeps the saved history position when an unseen delivery arrives after returning", () => {
    const entry = "reveal-returning-reader";
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, entry));
    resize();
    act(() => { viewport().scrollTop = 900; fireEvent.scroll(viewport()); });
    rerender(conversation(43, "reveal-other-agent"));
    rerender(conversation(42, entry));
    resize();
    expect(readingOffset("4.0")).toBe(0);

    rerender(conversation(42, entry, { itemId: "8.0", token: 1 }));
    resize();
    expect(viewport().scrollTop).toBe(900);
    expect(readingOffset("4.0")).toBe(0);
  });
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

describe("cross-reload reading position restore", () => {
  it("enforces 64-agent LRU cap, 7-day TTL, and malformed/unavailable storage safety", () => {
    const baseNow = 1_800_000_000_000;
    const browser = fakeBrowser({ session: {} });
    for (let i = 1; i <= RELOAD_BOOKMARK_MAX_AGENTS + 5; i++) {
      saveReloadBookmark(
        browser.session,
        String(i),
        {
          readingLocator: `loc-${i}`,
          itemId: `${i}.0`,
          rank: 0,
          viewportTop: 12,
          scrollTop: 300,
          savedAt: baseNow + i * 1000,
        },
        baseNow + i * 1000,
      );
    }
    const latestNow = baseNow + (RELOAD_BOOKMARK_MAX_AGENTS + 5) * 1000;
    expect(readReloadBookmark(browser.session, "1", latestNow)).toBeNull();
    expect(readReloadBookmark(browser.session, "5", latestNow)).toBeNull();
    expect(readReloadBookmark(browser.session, "6", latestNow)?.readingLocator).toBe("loc-6");
    expect(
      readReloadBookmark(browser.session, String(RELOAD_BOOKMARK_MAX_AGENTS + 5), latestNow)?.readingLocator,
    ).toBe(`loc-${RELOAD_BOOKMARK_MAX_AGENTS + 5}`);

    expect(
      readReloadBookmark(
        browser.session,
        String(RELOAD_BOOKMARK_MAX_AGENTS + 5),
        latestNow + RELOAD_BOOKMARK_TTL_MS + 1,
      ),
    ).toBeNull();

    browser.session.write(RELOAD_BOOKMARKS_STORAGE_KEY, "{broken-json");
    expect(readReloadBookmark(browser.session, "6", latestNow)).toBeNull();

    const unavailable = fakeBrowser({ session: {} });
    unavailable.refuseStorage(true);
    saveReloadBookmark(
      unavailable.session,
      "42",
      { itemId: "3.0", rank: 0, viewportTop: 0, scrollTop: 600, savedAt: baseNow },
      baseNow,
    );
    expect(readReloadBookmark(unavailable.session, "42", baseNow)).toBeNull();

    expect(
      matchReloadBookmark(
        { readingLocator: "old-epoch-loc", itemId: "3.0", rank: 0, viewportTop: 0, scrollTop: 600, savedAt: baseNow },
        [{ ...items[2], reading_locator: "new-epoch-loc" }],
        null,
      ),
    ).toBeNull();
  });

  it("prefers in-memory scrollMemory over sessionStorage within the open page", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      { readingLocator: "loc-3", itemId: "3.0", rank: 0, viewportTop: -100, scrollTop: 700, savedAt: 1000 },
      1000,
    );
    saveScrollMemory("same-tab-entry:agent:42", {
      contentKey: "42",
      scrollTop: 1500,
      followBottom: false,
      anchor: { itemId: "6.0", rank: 0, viewportTop: 0 },
    });
    const locatedItems = items.map((it, idx) => ({ ...it, reading_locator: `loc-${idx + 1}` }));
    renderWithBrowser(
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 2000}>
          <TimelineView
            items={locatedItems}
            threadKey="42"
            scrollMemoryKey="same-tab-entry:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 2000 }}
          />
        </WallClockProvider>
      </QueryClientProvider>,
      { browser },
    );
    resize();
    expect(viewport().scrollTop).toBe(1500);
    expect(readingOffset("6.0")).toBe(0);
  });

  it("restores tail and mid-conversation positions across reload (<= 1px) while keeping Working and live arrivals non-jumping", () => {
    const browser = fakeBrowser({ session: {} });
    let nowMs = 10_000;
    const locatedItems: BackendTimelineItem[] = items.map((it, idx) => ({
      ...it,
      reading_locator: `loc-${idx + 1}`,
    }));
    const mountReloadView = (
      bfcacheId: string,
      currentItems: BackendTimelineItem[],
      turnActive = false,
    ) => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => nowMs}>
          <TimelineView
            items={currentItems}
            threadKey="42"
            scrollMemoryKey={`${bfcacheId}:agent:42`}
            reloadRestore={{ state: "enabled", session: browser.session, now: () => nowMs }}
            turnActive={turnActive}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    // meiva:note
    // Mid-conversation scroll followed by simulated browser reload (unmount + fresh bfcacheId).
    const firstVisit = renderWithBrowser(mountReloadView("tab-load-1", locatedItems), { browser });
    resize();
    act(() => {
      viewport().scrollTop = 1015;
      fireEvent.scroll(viewport());
    });
    const savedMidOffset = readingOffset("5.0")!;
    firstVisit.unmount();

    top = 0;
    nowMs = 20_000;
    const midReload = renderWithBrowser(mountReloadView("tab-load-2", locatedItems), { browser });
    resize();
    expect(Math.abs((readingOffset("5.0") ?? 999) - savedMidOffset)).toBeLessThanOrEqual(1);
    expect(viewport().scrollTop).toBe(1015);
    expect(screen.queryByRole("alert")).toBeNull();

    // meiva:note
    // Tail reload keeps live Working row and streaming arrivals without yanking to bottom.
    act(() => {
      viewport().scrollTop = 4400;
      fireEvent.scroll(viewport());
    });
    const savedTailOffset = readingOffset("15.0")!;
    midReload.unmount();

    top = 0;
    nowMs = 30_000;
    const tailReload = renderWithBrowser(mountReloadView("tab-load-3", locatedItems, true), { browser });
    resize();
    expect(viewport().scrollTop).toBe(4400);
    expect(Math.abs((readingOffset("15.0") ?? 999) - savedTailOffset)).toBeLessThanOrEqual(1);

    const grownItems: BackendTimelineItem[] = [
      ...locatedItems,
      {
        item_id: "17.0",
        kind: "agent_chat",
        payload: "New live reply below restored tail",
        source: null,
        created_at: "2026-11-18T12:00:00Z",
        inbound_id: null,
        show_timestamp: true,
      },
      {
        item_id: "18.0",
        kind: "agent_reasoning",
        payload: "Analyzing live stream...",
        source: null,
        created_at: "2026-11-18T12:00:01Z",
        inbound_id: null,
        show_timestamp: false,
        reasoningStartedAt: nowMs - 1000,
      },
    ];
    act(() => {
      height = 5600;
      tailReload.rerender(mountReloadView("tab-load-3", grownItems, true));
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(viewport().scrollTop).toBe(4400);
    expect(Math.abs((readingOffset("15.0") ?? 999) - savedTailOffset)).toBeLessThanOrEqual(1);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Working");
    expect(screen.getByText("New live reply below restored tail")).toBeTruthy();
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();
  });

  it("silently backfills 3 older pages under ColdLoadSpinner and restores exact offset (<= 1px)", () => {
    const browser = fakeBrowser({ session: {} });
    const allItems: BackendTimelineItem[] = Array.from({ length: 16 }, (_, idx) => ({
      item_id: `${idx + 1}.0`,
      kind: "agent_chat",
      payload: `History row ${idx + 1}`,
      source: null,
      created_at: null,
      inbound_id: null,
      show_timestamp: false,
      reading_locator: `loc-${idx + 1}`,
    }));
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-2",
        itemId: "2.0",
        rank: 0,
        viewportTop: -60,
        scrollTop: 360,
        savedAt: 5000,
      },
      5000,
    );

    const origins: string[] = [];
    let sliceStart = 12;
    let loadingOlder = false;
    height = (16 - sliceStart) * 300;
    const onLoadOlder = vi.fn((origin: string) => {
      origins.push(origin);
      loadingOlder = true;
    });

    const renderSlice = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={allItems.slice(sliceStart)}
            threadKey="42"
            scrollMemoryKey="reload-older-pages:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
            hasMoreOlder={sliceStart > 0}
            loadingOlder={loadingOlder}
            readerLoadingOlder={false}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderSlice(), { browser });
    expect(origins).toEqual(["background"]);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("idle");

    // meiva:note
    // Page 1 arrives (rows 9..16).
    act(() => {
      sliceStart = 8;
      loadingOlder = false;
      height = (16 - sliceStart) * 300;
      view.rerender(renderSlice());
    });
    expect(origins).toEqual(["background", "background"]);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    // meiva:note
    // Page 2 arrives (rows 5..16).
    act(() => {
      sliceStart = 4;
      loadingOlder = false;
      height = (16 - sliceStart) * 300;
      view.rerender(renderSlice());
    });
    expect(origins).toEqual(["background", "background", "background"]);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    // meiva:note
    // Page 3 arrives (rows 1..16, includes loc-2).
    act(() => {
      sliceStart = 0;
      loadingOlder = false;
      height = 5000;
      view.rerender(renderSlice());
    });
    resize();
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(Math.abs((readingOffset("2.0") ?? 999) - -60)).toBeLessThanOrEqual(1);
    expect(viewport().scrollTop).toBe(360);
  });

  it("silently falls back to the live tail with zero error UI when bookmark exceeds page budget or is stale", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-compacted-away",
        itemId: "3.0",
        rank: 0,
        viewportTop: 0,
        scrollTop: 900,
        savedAt: 5000,
      },
      5000,
    );

    const pageItems = (startId: number): BackendTimelineItem[] =>
      Array.from({ length: 4 }, (_, idx) => ({
        item_id: `${startId + idx}.0`,
        kind: "agent_chat",
        payload: `Row ${startId + idx}`,
        source: null,
        created_at: null,
        inbound_id: null,
        show_timestamp: false,
        reading_locator: `loc-current-${startId + idx}`,
      }));

    let currentItems = pageItems(50);
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => {
      loadingOlder = true;
    });

    const renderBounded = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={currentItems}
            threadKey="42"
            scrollMemoryKey="reload-stale:agent:42"
            reloadRestore={{
              state: "enabled",
              session: browser.session,
              now: () => 6000,
              maxOlderPages: 2,
            }}
            hasMoreOlder
            loadingOlder={loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderBounded(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    act(() => {
      currentItems = [...pageItems(46), ...currentItems];
      loadingOlder = false;
      view.rerender(renderBounded());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);

    act(() => {
      currentItems = [...pageItems(42), ...currentItems];
      loadingOlder = false;
      view.rerender(renderBounded());
    });
    resize();

    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(viewport().scrollTop).toBe(4400);
    expect(readReloadBookmark(browser.session, "42", 6000)?.readingLocator).not.toBe("loc-compacted-away");
  });

  it("cancels background reload backfill immediately when the reader scrolls with the wheel", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      { readingLocator: "loc-ancient", itemId: "1.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: 5000 },
      5000,
    );
    const tailItems: BackendTimelineItem[] = items.slice(8).map((it, idx) => ({
      ...it,
      reading_locator: `loc-tail-${idx}`,
    }));
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => {
      loadingOlder = true;
    });
    const renderCancelable = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={tailItems}
            threadKey="42"
            scrollMemoryKey="reload-wheel-cancel:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
            hasMoreOlder
            loadingOlder={loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderCancelable(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    act(() => {
      fireEvent.wheel(viewport(), { deltaY: 120 });
      viewport().scrollTop = 500;
      fireEvent.scroll(viewport());
    });
    act(() => {
      loadingOlder = false;
      view.rerender(renderCancelable());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(viewport().scrollTop).toBe(500);
  });

  it("materializes a tall off-screen trailing anchor (content-visibility placeholder) when restoring deep negative viewportTop", () => {
    const browser = fakeBrowser({ session: {} });
    const locatedItems: BackendTimelineItem[] = items.map((it, idx) => ({
      ...it,
      reading_locator: `loc-cv-${idx + 1}`,
    }));
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-cv-16",
        itemId: "16.0",
        rank: 0,
        viewportTop: -1941.75,
        scrollTop: 6441.75,
        savedAt: 5000,
      },
      5000,
    );

    // meiva:note
    // Simulate content-visibility: auto intrinsic 96px height until 16.0 is materialized.
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      const tailNode = this.querySelector<HTMLElement>('.timeline-item[data-item-id="16.0"]');
      if (!tailNode) return 600;
      return tailNode.style.contentVisibility === "visible" ? 7041.5 : 5196;
    });
    renderWithBrowser(
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={locatedItems}
            threadKey="42"
            scrollMemoryKey="reload-cv-tail:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
          />
        </WallClockProvider>
      </QueryClientProvider>,
      { browser },
    );
    resize();
    const tailEl = viewport().querySelector<HTMLElement>('.timeline-item[data-item-id="16.0"]')!;
    expect(tailEl.style.contentVisibility).toBe("visible");
    expect(Math.abs(viewport().scrollTop - 6441.5)).toBeLessThanOrEqual(1);
  });

  it("ignores tail live SSE arrivals for page/item budgets during older-page backfill", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-older-target",
        itemId: "2.0",
        rank: 0,
        viewportTop: 0,
        scrollTop: 300,
        savedAt: 5000,
      },
      5000,
    );

    const makeRow = (id: string, loc: string): BackendTimelineItem => ({
      item_id: id,
      kind: "agent_chat",
      payload: `Row ${id}`,
      source: null,
      created_at: null,
      inbound_id: null,
      show_timestamp: false,
      reading_locator: loc,
    });

    let currentItems: BackendTimelineItem[] = [
      makeRow("10.0", "loc-10"),
      makeRow("11.0", "loc-11"),
    ];
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => {
      loadingOlder = true;
    });

    const renderLiveBackfill = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={currentItems}
            threadKey="42"
            scrollMemoryKey="reload-live-tail-sse:agent:42"
            reloadRestore={{
              state: "enabled",
              session: browser.session,
              now: () => 6000,
              maxOlderPages: 2,
              maxOlderItems: 6,
            }}
            turnActive
            hasMoreOlder
            loadingOlder={loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderLiveBackfill(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // meiva:note
    // Tail SSE arrivals while loadingOlder is active must not consume maxOlderItems (6) or maxOlderPages (2).
    act(() => {
      const liveTail = Array.from({ length: 12 }, (_, idx) =>
        makeRow(`${12 + idx}.0`, `loc-live-${12 + idx}`),
      );
      currentItems = [...currentItems, ...liveTail];
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    // meiva:note
    // Page 1 prepends 3 older items (still < maxOlderItems=6 despite 12 tail SSE items) and triggers page 2.
    act(() => {
      currentItems = [
        makeRow("7.0", "loc-7"),
        makeRow("8.0", "loc-8"),
        makeRow("9.0", "loc-9"),
        ...currentItems,
      ];
      loadingOlder = false;
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);

    // meiva:note
    // Page 2 prepends the target bookmark row (`loc-older-target`) and lands <= 1px.
    act(() => {
      currentItems = [
        makeRow("2.0", "loc-older-target"),
        makeRow("6.0", "loc-6"),
        ...currentItems,
      ];
      loadingOlder = false;
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    resize();
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(Math.abs(readingOffset("2.0") ?? 999)).toBeLessThanOrEqual(1);
  });

  it("requests older history only once and falls back silently when onLoadOlder fails alongside tail live SSE", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-unreachable-on-503",
        itemId: "2.0",
        rank: 0,
        viewportTop: 0,
        scrollTop: 300,
        savedAt: 5000,
      },
      5000,
    );

    const makeRow = (id: string, loc: string): BackendTimelineItem => ({
      item_id: id,
      kind: "agent_chat",
      payload: `Row ${id}`,
      source: null,
      created_at: null,
      inbound_id: null,
      show_timestamp: false,
      reading_locator: loc,
    });

    let currentItems: BackendTimelineItem[] = [
      makeRow("10.0", "loc-10"),
      makeRow("11.0", "loc-11"),
    ];
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => {
      loadingOlder = true;
    });

    const renderFailingBackfill = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={currentItems}
            threadKey="42"
            scrollMemoryKey="reload-fail-with-sse:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
            turnActive
            hasMoreOlder
            loadingOlder={loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderFailingBackfill(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // meiva:note
    // Simulate in-flight loadingOlder transition followed by HTTP 503 failure (`loadingOlder -> false`)
    // on the same commit a new live SSE item appends to the tail (`currentOldest` unchanged at `10.0`).
    act(() => {
      view.rerender(renderFailingBackfill());
    });
    act(() => {
      loadingOlder = false;
      currentItems = [...currentItems, makeRow("12.0", "loc-live-12")];
      view.rerender(renderFailingBackfill());
    });
    resize();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(viewport().scrollTop).toBe(4400);
    expect(readReloadBookmark(browser.session, "42", 6000)?.readingLocator).not.toBe(
      "loc-unreachable-on-503",
    );
  });

  it("reuses findOldestTimelineCursor across fresh post-compact windows containing only inbound_compact_summary", () => {
    const postCompactHead: BackendTimelineItem[] = [
      {
        item_id: "0.0",
        kind: "system_prompt",
        payload: "System prompt",
        source: null,
        created_at: null,
        inbound_id: null,
        show_timestamp: false,
      },
      {
        item_id: "1.0",
        kind: "inbound_compact_summary",
        payload: "Compacted summary",
        source: "compact",
        created_at: null,
        inbound_id: 1,
        show_timestamp: false,
        reading_locator: "loc-compact-summary",
      },
    ];
    expect(oldestReloadCursorId(postCompactHead)).toBe("1.0");

    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(
      browser.session,
      "42",
      {
        readingLocator: "loc-pre-compact-msg",
        itemId: "8.0",
        rank: 0,
        viewportTop: 0,
        scrollTop: 300,
        savedAt: 5000,
      },
      5000,
    );

    let currentItems = postCompactHead;
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => {
      loadingOlder = true;
    });

    const renderPostCompact = () => (
      <QueryClientProvider client={client}>
        <WallClockProvider wallNow={() => 6000}>
          <TimelineView
            items={currentItems}
            threadKey="42"
            scrollMemoryKey="reload-post-compact:agent:42"
            reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
            hasMoreOlder
            loadingOlder={loadingOlder}
            onLoadOlder={onLoadOlder}
          />
        </WallClockProvider>
      </QueryClientProvider>
    );

    const view = renderWithBrowser(renderPostCompact(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    act(() => {
      currentItems = [
        postCompactHead[0],
        {
          item_id: "8.0@1",
          kind: "agent_chat",
          payload: "Pre-compact paragraph",
          source: null,
          created_at: null,
          inbound_id: null,
          show_timestamp: false,
          reading_locator: "loc-pre-compact-msg",
        },
        postCompactHead[1],
      ];
      loadingOlder = false;
      view.rerender(renderPostCompact());
    });
    resize();

    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(Math.abs(readingOffset("8.0@1") ?? 999)).toBeLessThanOrEqual(1);
  });
});
