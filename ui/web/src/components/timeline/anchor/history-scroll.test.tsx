import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { Profiler, StrictMode, type ReactNode, type RefObject } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type TimelineResponse } from "@/lib/contracts/types";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { resetTransport, transport } from "@/test-support/fake-api";
import { ApiProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { fakeBrowser } from "@/lib/browser/testing";
import { TimelineView, type RevealDecision } from "..";
import { READING_TOP_PX } from "./dom";
import { writeMemoryPlace, type SavedPlace } from "./saved";
import {
  readReloadBookmark,
  RELOAD_BOOKMARK_MAX_AGENTS,
  RELOAD_BOOKMARK_TTL_MS,
  RELOAD_BOOKMARKS_STORAGE_KEY,
  RELOAD_RESTORE_SEARCH_TIMEOUT_MS,
  saveReloadBookmark,
} from "../model/reading-position";
import { TestStreamProvider } from "@/test-support/fake-stream";

beforeEach(() => {
  resetTransport();
  transport.getConfig.mockResolvedValue({ fields: [] });
});
vi.mock("../../content/markdown", () => ({ ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock("../../ui/scroll-area", () => ({
  ScrollArea: ({
    children,
    viewportRef,
    ...rest
  }: {
    children: ReactNode;
    viewportRef?: RefObject<HTMLDivElement | null>;
    "data-activity-scroll"?: string;
  }) =>
    rest["data-activity-scroll"] === "true" ? (
      <div data-activity-scroll="true"><div>{children}</div></div>
    ) : (
      <div><div ref={viewportRef} data-testid="history-viewport" data-slot="scroll-area-viewport">{children}</div></div>
    ),
}));
const items: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
  item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
  source: null, created_at: null, inbound_id: null, show_timestamp: false,
}));
const response: TimelineResponse = { items, msg_count: 17, has_more: false };
// meiva:note
// Geometry: every row is 300px tall and sits at its position among the mounted rows (so a prepend
// pushes the rows below it down), shifted by `aboveSettle`; the log is `height` tall, the reserve
// follows it, and the viewport is 600px.
let top = 0;
let height = 5000;
let aboveSettle = 0;
let client: QueryClient;
let resizeCallbacks: Set<() => void>;
// meiva:note
// Every commit of the view, read after its layout effects: whether the rows could be seen, and where.
let frames: { shown: boolean; top: number }[];
// meiva:note
// The timeline's config read is the only transport call this file needs; everything else
// must stay unanswered so an accidental read fails loudly.
const gateway = transport;

function recordFrame() {
  const column = document.querySelector<HTMLElement>("[data-timeline-column]");
  if (column) frames.push({ shown: column.style.visibility !== "hidden", top });
}
const shownTops = () => [...new Set(frames.filter((frame) => frame.shown).map((frame) => frame.top))];

function Conversation({ agentId, entry, reveal }: { agentId: number | null; entry: string; reveal?: RevealDecision }) {
  const timeline = useTimeline(agentId, () => undefined);
  return <TimelineView items={timeline.items} threadKey={agentId === null ? undefined : String(agentId)} scrollMemoryKey={entry} reveal={reveal} />;
}
const wallNow = () => Date.now();
function conversation(agentId: number | null, entry: string, reveal?: RevealDecision) {
  return (
    <QueryClientProvider client={client}>
      <WallClockProvider wallNow={wallNow}>
        <ApiProvider api={gateway}>
          <TestStreamProvider>
            <Profiler id="timeline" onRender={recordFrame}>
              <Conversation agentId={agentId} entry={entry} reveal={reveal} key={entry} />
            </Profiler>
          </TestStreamProvider>
        </ApiProvider>
      </WallClockProvider>
    </QueryClientProvider>
  );
}
function host(ui: ReactNode, now: () => number = () => 6000) {
  return (
    <QueryClientProvider client={client}>
      <WallClockProvider wallNow={now}>
        <Profiler id="timeline" onRender={recordFrame}>{ui}</Profiler>
      </WallClockProvider>
    </QueryClientProvider>
  );
}
const shell = () => <QueryClientProvider client={client}><div>Shell page</div></QueryClientProvider>;
function viewport() { return screen.getByTestId("history-viewport"); }
function column() { return viewport().querySelector<HTMLElement>("[data-timeline-column]")!; }
function rowsShown() { return column().style.visibility !== "hidden"; }
function reserve() { return viewport().querySelector<HTMLElement>("[data-timeline-reserve]")!; }
function readingOffset(id: string) {
  const node = viewport().querySelector(`.timeline-item[data-item-id="${CSS.escape(id)}"]`);
  return node ? Math.round(node.getBoundingClientRect().top - viewport().getBoundingClientRect().top) : null;
}
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }
function readerScroll(to: number) { act(() => { viewport().scrollTop = to; fireEvent.scroll(viewport()); }); }
// meiva:note
// Anchor saves are batched to the next frame.
async function nextFrame() { await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))); }
const place = (itemId: string, offset: number, extra: Partial<SavedPlace> = {}): SavedPlace => ({
  identity: { row: { itemId, rank: 0 } }, offset, holdReserve: false, ...extra,
});
const rect = (y: number, h: number) => ({ top: y, bottom: y + h, height: h, left: 0, right: 400, width: 400, x: 0, y }) as DOMRect;
function rowIndex(node: HTMLElement) {
  const scroller = node.closest('[data-slot="scroll-area-viewport"]');
  return scroller ? [...scroller.querySelectorAll("[data-item-id]")].indexOf(node) : 0;
}
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
  frames = [];
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
    return height + [...this.querySelectorAll<HTMLElement>("[data-timeline-reserve]")]
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
    if (this.dataset.slot === "scroll-area-viewport") return rect(0, 600);
    if (this.getAttribute("role") === "log") return rect(-top, height);
    if (this.hasAttribute("data-timeline-reserve")) return rect(height - top, this.offsetHeight);
    if (this.dataset.itemId) return rect(rowIndex(this) * 300 - top + aboveSettle, 300);
    return rect(0, 300);
  });
});
afterEach(() => {
  cleanup(); client.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();
});

describe("returning to a conversation in the same page", () => {
  it.each([false, true])("shows the saved row at its offset in the first visible frame (live store warm: %s)", (warm) => {
    if (warm) useTimelineStore.getState().switchThread(42, items, false);
    const entry = `history-return-${warm}`;
    writeMemoryPlace(entry, "42", place("3.0", -100));
    renderWithBrowser(conversation(42, entry));
    expect(rowsShown()).toBe(true);
    expect(viewport().scrollTop).toBe(700);
    expect(readingOffset("3.0")).toBe(-100);
    expect(shownTops()).toEqual([700]);
  });

  it("lands on a retained row while the authority read is still pending", () => {
    writeMemoryPlace("history-pending", "42", place("3.0", -100));
    renderWithBrowser(host(<TimelineView items={items} threadKey="42" scrollMemoryKey="history-pending" loading />));
    expect(rowsShown()).toBe(true);
    expect(viewport().scrollTop).toBe(700);
    expect(shownTops()).toEqual([700]);
  });

  it("keeps the rows hidden under the ring until the saved row arrives", () => {
    writeMemoryPlace("history-cold-rows", "42", place("3.0", -100));
    const view = renderWithBrowser(host(<TimelineView items={[]} threadKey="42" scrollMemoryKey="history-cold-rows" loading />));
    expect(rowsShown()).toBe(false);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();
    view.rerender(host(<TimelineView items={items} threadKey="42" scrollMemoryKey="history-cold-rows" />));
    expect(rowsShown()).toBe(true);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(viewport().scrollTop).toBe(700);
    expect(shownTops()).toEqual([700]);
  });

  it.each([false, true])("keeps the saved row while a late layout grows the content (strict: %s)", (strict) => {
    height = 900;
    const entry = `history-partial-${strict}`;
    writeMemoryPlace(entry, "42", place("3.0", -100));
    renderWithBrowser(strict ? <StrictMode>{conversation(42, entry)}</StrictMode> : conversation(42, entry));
    resize();
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
    expect(readingOffset("3.0")).toBe(-100);
  });

  it("restores through a strict-mode effect re-attach", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    writeMemoryPlace("history-strict", "42", place("3.0", -100));
    renderWithBrowser(<StrictMode>{conversation(42, "history-strict")}</StrictMode>);
    expect(viewport().scrollTop).toBe(700);
  });

  it("keeps independent places for two agents in one page entry", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-tabs"));
    readerScroll(700);
    rerender(tabs(43, "agent-tabs"));
    readerScroll(1200);
    rerender(tabs(42, "agent-tabs"));
    expect(viewport().scrollTop).toBe(700);
    rerender(tabs(43, "agent-tabs"));
    expect(viewport().scrollTop).toBe(1200);
  });

  it("keeps independent places across entry unmounts and returns", () => {
    const { rerender } = renderWithBrowser(conversation(42, "history-first"));
    readerScroll(700);
    rerender(conversation(43, "history-second"));
    readerScroll(1200);
    rerender(shell());
    top = 0;
    rerender(conversation(42, "history-first"));
    expect(viewport().scrollTop).toBe(700);
    rerender(shell());
    top = 0;
    rerender(conversation(43, "history-second"));
    expect(viewport().scrollTop).toBe(1200);
  });

  it("remembers a returned reader's later scroll across a switch", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    writeMemoryPlace("history-later", "42", place("3.0", -100));
    const { rerender } = renderWithBrowser(conversation(42, "history-later"));
    readerScroll(1200);
    rerender(conversation(43, "history-later-other"));
    top = 0;
    rerender(conversation(42, "history-later"));
    expect(viewport().scrollTop).toBe(1200);
    expect(readingOffset("5.0")).toBe(0);
  });

  it.each(["grows while away", "grows after the return", "grows between a chunk and the switch"])(
    "returns to the tail when only the layout changed (%s)",
    (when) => {
      const entry = `agent-tail-${when}`;
      const { rerender } = renderWithBrowser(tabs(42, entry));
      expect(viewport().scrollTop).toBe(4400);
      if (when === "grows between a chunk and the switch") act(() => { height = 6000; });
      rerender(tabs(43, entry));
      if (when === "grows while away") act(() => { height = 6000; });
      top = 0;
      rerender(tabs(42, entry));
      expect(viewport().scrollTop).toBe(when === "grows after the return" ? 4400 : 5400);
      act(() => { height = 7000; resizeCallbacks.forEach((deliver) => deliver()); });
      expect(viewport().scrollTop).toBe(6400);
      expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
    },
  );

  it("saves the row on screen inside the wider touch bottom zone", () => {
    const browser = fakeBrowser({ media: { "(pointer: coarse)": true } });
    const { rerender } = renderWithBrowser(tabs(42, "agent-touch-zone"), { browser });
    readerScroll(4300);
    rerender(tabs(43, "agent-touch-zone"));
    top = 0;
    rerender(tabs(42, "agent-touch-zone"));
    expect(viewport().scrollTop).toBe(4300);
    expect(readingOffset("15.0")).toBe(-100);
  });

  it("returns a reader in history to their row after messages arrived while away", () => {
    const { rerender } = renderWithBrowser(tabs(42, "agent-new-parked"));
    readerScroll(4300);
    rerender(tabs(43, "agent-new-parked"));
    act(() => { height = 6000; });
    rerender(tabs(42, "agent-new-parked"));
    resize();
    expect(viewport().scrollTop).toBe(4300);
    expect(readingOffset("15.0")).toBe(-100);
  });

  it("keeps a returned reader in place when content grows", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    writeMemoryPlace("history-growth", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-growth"));
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });

  it.each(["fresh-entry", "mismatched-content"])("follows the tail after %s", (entry) => {
    if (entry === "mismatched-content") writeMemoryPlace(entry, "43", place("3.0", -100));
    renderWithBrowser(conversation(42, entry));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });

  it("lets a reader's scroll replace a saved place the layout could not yet hold", () => {
    height = 900;
    writeMemoryPlace("history-short-reader", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-short-reader"));
    resize();
    readerScroll(100);
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(100);
  });

  it("keeps the saved place if the entry leaves before its layout can hold it", () => {
    height = 900;
    writeMemoryPlace("history-short-leave", "42", place("3.0", -100));
    const { rerender } = renderWithBrowser(conversation(42, "history-short-leave"));
    resize();
    act(() => { fireEvent.scroll(viewport()); });
    rerender(shell());
    top = 0; height = 5000;
    rerender(conversation(42, "history-short-leave"));
    expect(viewport().scrollTop).toBe(700);
  });

  it("keeps the saved place through an upward wheel notch that does not scroll", () => {
    height = 900;
    writeMemoryPlace("history-short-wheel", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-short-wheel"));
    resize();
    act(() => { fireEvent.wheel(viewport(), { deltaY: -100 }); });
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });

  it("retains scroll-loaded history and the reader's row across a return and a newer tail snapshot", () => {
    const older: BackendTimelineItem = { ...items[0], item_id: "s1.old.1.0", payload: "Previously loaded history" };
    const { rerender } = renderWithBrowser(tabs(42, "agent-old-window"));
    act(() => { height = 5300; useTimelineStore.getState().prependOlder([older], true); });
    readerScroll(1000);
    expect(readingOffset("3.0")).toBe(-100);
    rerender(tabs(43, "agent-old-window"));
    expect(screen.queryByText("Previously loaded history")).toBeNull();
    rerender(tabs(42, "agent-old-window"));
    expect(screen.getByText("Previously loaded history")).toBeTruthy();
    expect(viewport().scrollTop).toBe(1000);
    act(() => {
      height = 5600;
      useTimelineStore.getState().reloadSnapshot([...items, { ...items[0], item_id: "17.0", payload: "New reply" }], 18, false);
    });
    resize();
    expect(screen.getByText("Previously loaded history")).toBeTruthy();
    expect(readingOffset("3.0")).toBe(-100);
    expect(viewport().scrollTop).toBe(1000);
  });

  it("keeps the down arrow visible during an older-history load", () => {
    renderWithBrowser(host(<TimelineView items={items} threadKey="one-ring" loadingOlder readerLoadingOlder />));
    readerScroll(700);
    expect(screen.getByRole("button", { name: "Scroll down" }).querySelector(".animate-spin")).toBeNull();
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("loading");
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
          <TestStreamProvider>
            <Profiler id="timeline" onRender={recordFrame}>
              <AgentTabs agentId={agentId} entry={entry} turnActive={turnActive} />
            </Profiler>
          </TestStreamProvider>
        </ApiProvider>
      </WallClockProvider>
    </QueryClientProvider>
  );
}

describe("a toggle reserve and growth below a returned reader", () => {
  it("recreates the space a header toggle used for a saved place beyond the shortened conversation", () => {
    writeMemoryPlace("agent-collapse-space:agent:42", "42", place("16.0", -100, { holdReserve: true }));
    const { rerender } = renderWithBrowser(tabs(43, "agent-collapse-space"));
    rerender(tabs(42, "agent-collapse-space"));
    resize();
    expect(viewport().scrollTop).toBe(4600);
    expect(readingOffset("16.0")).toBe(-100);
    expect(reserve().style.height).toBe("200px");
  });

  it("leaves a returned reader in history at their place while the conversation grows", () => {
    height = 900;
    writeMemoryPlace("history-send", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-send"));
    resize();
    act(() => { height = 5000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(700);
  });

  it("keeps a returned row at the bottom in place while the conversation grows", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    writeMemoryPlace("history-tail-send", "42", place("15.0", -200));
    renderWithBrowser(conversation(42, "history-tail-send"));
    expect(viewport().scrollTop).toBe(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(4400);
  });

  it("follows again once a returned reader reaches the end", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    writeMemoryPlace("history-hold-release", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-hold-release"));
    expect(viewport().scrollTop).toBe(700);
    readerScroll(4400);
    act(() => { height = 6000; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(5400);
  });
});

describe("layout changes keep the reader's row", () => {
  it("keeps a returned row at its saved offset while the rows above settle", () => {
    writeMemoryPlace("history-settle", "42", place("3.0", -100));
    renderWithBrowser(conversation(42, "history-settle"));
    expect(viewport().scrollTop).toBe(700);
    act(() => { aboveSettle = -50; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(650);
    expect(readingOffset("3.0")).toBe(-100);
    act(() => { aboveSettle = -80; resizeCallbacks.forEach((deliver) => deliver()); });
    expect(viewport().scrollTop).toBe(620);
    expect(readingOffset("3.0")).toBe(-100);
  });

  it("keeps the row through a switch away while the rows above settle", () => {
    writeMemoryPlace("history-settle-switch", "42", place("3.0", -100));
    const { rerender } = renderWithBrowser(conversation(42, "history-settle-switch"));
    act(() => { aboveSettle = -50; resizeCallbacks.forEach((deliver) => deliver()); });
    act(() => { fireEvent.scroll(viewport()); });
    rerender(conversation(43, "history-settle-other"));
    rerender(conversation(42, "history-settle-switch"));
    expect(viewport().scrollTop).toBe(650);
    expect(readingOffset("3.0")).toBe(-100);
  });

  const withImage = () => {
    const rows = items.map<BackendTimelineItem>((item, index) => index === 0
      ? { ...item, kind: "inbound_chat", source: "user", images: ["/picture.png"] } : item);
    client.setQueryData(["timeline", 42], { ...response, items: rows });
    return rows;
  };

  it.each(["load", "error"])("keeps a reader on their row after an image above it fires %s", (event) => {
    useTimelineStore.getState().switchThread(42, withImage(), false);
    renderWithBrowser(conversation(42, `history-media-${event}`));
    resize();
    readerScroll(900);
    expect(readingOffset("4.0")).toBe(0);
    act(() => {
      aboveSettle = event === "load" ? 120 : -20;
      fireEvent(screen.getByAltText("attached image"), new Event(event));
    });
    resize();
    expect(readingOffset("4.0")).toBe(0);
    expect(viewport().scrollTop).toBe(event === "load" ? 1020 : 880);
  });

  it("uses the reader's latest row when they scroll before a queued image decode lands", () => {
    withImage();
    renderWithBrowser(conversation(42, "history-media-navigation"));
    resize();
    readerScroll(900);
    act(() => {
      fireEvent.load(screen.getByAltText("attached image"));
      viewport().scrollTop = 1200;
      fireEvent.scroll(viewport());
      aboveSettle = 120;
    });
    resize();
    expect(readingOffset("5.0")).toBe(0);
    expect(viewport().scrollTop).toBe(1320);
    act(() => { aboveSettle = 240; fireEvent.load(screen.getByAltText("attached image")); });
    resize();
    expect(readingOffset("5.0")).toBe(0);
    readerScroll(4400);
    act(() => { aboveSettle = 360; fireEvent.load(screen.getByAltText("attached image")); });
    resize();
    expect(viewport().scrollTop).toBe(4400);
  });

  it("keeps the reader on their row when older history is prepended above it", () => {
    const older: BackendTimelineItem = { ...items[0], item_id: "s1.old.1.0", payload: "Older page row" };
    renderWithBrowser(tabs(42, "history-prepend"));
    readerScroll(700);
    expect(readingOffset("3.0")).toBe(-100);
    act(() => { height = 5300; useTimelineStore.getState().prependOlder([older], true); });
    expect(screen.getByText("Older page row")).toBeTruthy();
    expect(readingOffset("3.0")).toBe(-100);
    expect(viewport().scrollTop).toBe(1000);
  });

  it("keeps the reader on their row when the viewport width re-wraps the rows above", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    renderWithBrowser(conversation(42, "history-width-change"));
    resize();
    readerScroll(900);
    expect(readingOffset("4.0")).toBe(0);
    let width = 400;
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
    act(() => {
      width = 700;
      aboveSettle = 120;
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(readingOffset("4.0")).toBe(0);
    expect(viewport().scrollTop).toBe(1020);
  });
});

describe("first visit and the reveal", () => {
  it("keeps the rows hidden until the reveal is decided, then opens on the revealed row", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, "reveal-entry", { decided: false, itemId: null }));
    expect(rowsShown()).toBe(false);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();
    rerender(conversation(42, "reveal-entry", { decided: true, itemId: "8.0" }));
    expect(rowsShown()).toBe(true);
    expect(readingOffset("8.0")).toBe(READING_TOP_PX);
    expect(viewport().scrollTop).toBe(7 * 300 - READING_TOP_PX);
    expect(shownTops()).toEqual([7 * 300 - READING_TOP_PX]);
  });

  it("opens the tail when the reveal stays undecided past the time bound", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    useTimelineStore.getState().switchThread(42, items, false);
    renderWithBrowser(conversation(42, "reveal-undecided", { decided: false, itemId: null }));
    act(() => { vi.advanceTimersByTime(RELOAD_RESTORE_SEARCH_TIMEOUT_MS - 1); });
    expect(rowsShown()).toBe(false);
    act(() => { vi.advanceTimersByTime(1); });
    expect(rowsShown()).toBe(true);
    expect(viewport().scrollTop).toBe(4400);
    expect(shownTops()).toEqual([4400]);
  });

  it("opens the tail when the revealed row is not loaded", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    renderWithBrowser(conversation(42, "reveal-missing", { decided: true, itemId: "99.0" }));
    resize();
    expect(viewport().scrollTop).toBe(4400);
  });

  it("does not move a reader when a reveal is decided after the conversation opened", () => {
    useTimelineStore.getState().switchThread(42, items, false);
    const { rerender } = renderWithBrowser(conversation(42, "reveal-late"));
    resize();
    expect(viewport().scrollTop).toBe(4400);
    rerender(conversation(42, "reveal-late", { decided: true, itemId: "8.0" }));
    resize();
    expect(viewport().scrollTop).toBe(4400);
  });

  it.each([[4400, 0], [900, 900]])("prioritizes the reveal only after leaving at tail (left at %i)", (leftAt, expected) => {
    useTimelineStore.getState().switchThread(42, items, false);
    const entry = `reveal-returning-${leftAt}`;
    const { rerender } = renderWithBrowser(conversation(42, entry));
    resize();
    if (leftAt !== 4400) readerScroll(leftAt);
    rerender(conversation(43, `${entry}-other`));
    top = 0;
    rerender(conversation(42, entry, { decided: true, itemId: "1.0" }));
    resize();
    expect(viewport().scrollTop).toBe(expected);
    expect(screen.queryByTestId("scroll-to-bottom")).not.toBeNull();
  });
});

describe("bounded unread return geometry", () => {
  const history = items[0];
  const human: BackendTimelineItem = { ...items[1], kind: "inbound_chat", source: "user", payload: "My message" };
  const reply = items[2];
  const viewportHeight = 1000;
  const dockOverlap = 80;
  const historyHeight = 2000;
  let replyHeight: number;

  function mountReturn(initialHeight: number) {
    replyHeight = initialHeight;
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(viewportHeight);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.querySelector('[data-item-id="3.0"]')
        ? historyHeight + 100 + replyHeight + dockOverlap
        : historyHeight + 100 + dockOverlap;
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      switch (this.dataset.itemId) {
        case "1.0": return rect(-top, historyHeight);
        case "2.0": return rect(historyHeight - top, 100);
        case "3.0": return rect(historyHeight + 100 - top, replyHeight);
      }
      if (this.getAttribute("role") === "log") {
        const total = historyHeight + 100 + (this.querySelector('[data-item-id="3.0"]') ? replyHeight : 0);
        return rect(-top, total);
      }
      return rect(0, viewportHeight);
    });
    const entry = `bounded-geometry-${initialHeight}`;
    const view = renderWithBrowser(host(<TimelineView items={[history, human]} threadKey="42" scrollMemoryKey={entry} bottomOverlayHeight={dockOverlap} />));
    view.rerender(shell());
    top = 0;
    frames.length = 0;
    view.rerender(host(<TimelineView items={[history, human, reply]} threadKey="42" scrollMemoryKey={entry} bottomOverlayHeight={dockOverlap} />));
  }

  it.each([false, true])("brings a prompt from an older page into the first visible return (delivered: %s)", (delivered) => {
    const work: BackendTimelineItem = { ...items[3], item_id: "2.1", kind: "agent_reasoning" };
    const loadOlder = vi.fn();
    const entry = `paged-boundary-${delivered}`;
    const opening = [history, human];
    const reveal = { decided: true, itemId: delivered ? reply.item_id : null };
    const presenting = (rows: BackendTimelineItem[], loadingOlder = false) => host(<TimelineView items={rows}
      threadKey="42" scrollMemoryKey={entry} hasMoreOlder onLoadOlder={loadOlder} loadingOlder={loadingOlder} reveal={reveal} />);
    const view = renderWithBrowser(host(<TimelineView items={opening} threadKey="42" scrollMemoryKey={entry} />));
    view.rerender(shell());
    top = 0;
    frames.length = 0;
    view.rerender(presenting([work, reply]));
    expect(rowsShown()).toBe(false);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    view.rerender(presenting([work, reply], true));
    view.rerender(presenting([...opening, work, reply]));
    expect(rowsShown()).toBe(true);
    expect(readingOffset(human.item_id)).toBe(READING_TOP_PX);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    expect(shownTops()).toEqual([300 - READING_TOP_PX]);
  });

  it.each([false, true])("treats reattached standing context as a page edge and respects a real wake (%s)", (wake) => {
    const system: BackendTimelineItem = { ...history, item_id: "0.0", kind: "system_prompt" };
    const note: BackendTimelineItem = { ...history, item_id: "0.1", kind: "system_marker", source: "agent_id" };
    const summary: BackendTimelineItem = { ...history, item_id: "0.2", kind: "inbound_compact_summary" };
    const scheduled: BackendTimelineItem = { ...history, item_id: "2.1", kind: "inbound_chat", source: "schedule:2" };
    const work: BackendTimelineItem = { ...history, item_id: "2.2", kind: "agent_reasoning" };
    const partial = { ...reply, partial: true, payload: "A short reply" };
    const head = [system, note, summary];
    const suffix = [...(wake ? [scheduled] : []), work];
    const latest = [...head, ...suffix, reply];
    const loadOlder = vi.fn();
    const entry = `paged-standing-boundary-${wake}`;
    const view = renderWithBrowser(host(<TimelineView items={[...latest.slice(0, -1), partial]}
      threadKey="42" scrollMemoryKey={entry} />));
    view.rerender(shell());
    top = 0;
    const present = (rows: BackendTimelineItem[]) => host(<TimelineView items={rows}
      threadKey="42" scrollMemoryKey={entry} hasMoreOlder onLoadOlder={loadOlder} />);
    view.rerender(present(latest));
    expect(loadOlder).toHaveBeenCalledTimes(wake ? 0 : 1);
    if (!wake) {
      expect(rowsShown()).toBe(false);
      view.rerender(present([...head, history, human, ...suffix, reply]));
    }
    expect(rowsShown()).toBe(true);
    expect(readingOffset(wake ? reply.item_id : human.item_id)).toBe(READING_TOP_PX);
    expect(loadOlder).toHaveBeenCalledTimes(wake ? 0 : 1);
  });

  it("opens a 1500px reply with the 100px prompt at the safe top of a 1000px viewport", () => {
    mountReturn(1500);
    expect(readingOffset(human.item_id)).toBe(READING_TOP_PX);
    expect(readingOffset(reply.item_id)).toBe(READING_TOP_PX + 100);
    expect(shownTops()).toEqual([historyHeight - READING_TOP_PX]);
  });

  it("fits a 100px prompt and 100px reply above the dock and stops following at the safe top as the reply grows", () => {
    mountReturn(100);
    expect(readingOffset(reply.item_id)! + 100).toBe(viewportHeight - dockOverlap);
    expect(readingOffset(human.item_id)).toBe(viewportHeight - dockOverlap - 200);
    replyHeight = 500;
    resize();
    expect(readingOffset(reply.item_id)! + replyHeight).toBe(viewportHeight - dockOverlap);
    replyHeight = 1500;
    resize();
    expect(readingOffset(human.item_id)).toBe(READING_TOP_PX);
    replyHeight = 2000;
    resize();
    expect(readingOffset(human.item_id)).toBe(READING_TOP_PX);
    readerScroll(top + 150);
    expect(viewport().hasAttribute("data-unread-bounded")).toBe(false);
    replyHeight = 2500;
    resize();
    expect(readingOffset(human.item_id)).toBe(READING_TOP_PX - 150);
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
        { readingLocator: `loc-${i}`, itemId: `${i}.0`, rank: 0, viewportTop: 12, scrollTop: 300, savedAt: baseNow + i * 1000 },
        baseNow + i * 1000,
      );
    }
    const latestNow = baseNow + (RELOAD_BOOKMARK_MAX_AGENTS + 5) * 1000;
    expect(readReloadBookmark(browser.session, "1", latestNow)).toBeNull();
    expect(readReloadBookmark(browser.session, "5", latestNow)).toBeNull();
    expect(readReloadBookmark(browser.session, "6", latestNow)?.readingLocator).toBe("loc-6");
    expect(readReloadBookmark(browser.session, String(RELOAD_BOOKMARK_MAX_AGENTS + 5), latestNow)?.readingLocator)
      .toBe(`loc-${RELOAD_BOOKMARK_MAX_AGENTS + 5}`);
    expect(readReloadBookmark(browser.session, String(RELOAD_BOOKMARK_MAX_AGENTS + 5), latestNow + RELOAD_BOOKMARK_TTL_MS + 1)).toBeNull();

    browser.session.write(RELOAD_BOOKMARKS_STORAGE_KEY, "{broken-json");
    expect(readReloadBookmark(browser.session, "6", latestNow)).toBeNull();

    const unavailable = fakeBrowser({ session: {} });
    unavailable.refuseStorage(true);
    saveReloadBookmark(unavailable.session, "42", { itemId: "3.0", rank: 0, viewportTop: 0, scrollTop: 600, savedAt: baseNow }, baseNow);
    expect(readReloadBookmark(unavailable.session, "42", baseNow)).toBeNull();
  });

  it("prefers the in-page memory place over the reload bookmark", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-3", itemId: "3.0", rank: 0, viewportTop: -100, scrollTop: 700, savedAt: 1000 }, 1000);
    writeMemoryPlace("same-tab-entry:agent:42", "42", place("6.0", 0));
    const locatedItems = items.map((it, idx) => ({ ...it, reading_locator: `loc-${idx + 1}` }));
    renderWithBrowser(host(
      <TimelineView items={locatedItems} threadKey="42" scrollMemoryKey="same-tab-entry:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 2000 }} />,
      () => 2000,
    ), { browser });
    resize();
    expect(viewport().scrollTop).toBe(1500);
    expect(readingOffset("6.0")).toBe(0);
  });

  it("restores mid-conversation and near-tail rows across reload, keeps live arrivals still, and reopens a tail reader on the tail", async () => {
    const browser = fakeBrowser({ session: {} });
    let nowMs = 10_000;
    const locatedItems: BackendTimelineItem[] = items.map((it, idx) => ({ ...it, reading_locator: `loc-${idx + 1}` }));
    const mountReloadView = (bfcacheId: string, currentItems: BackendTimelineItem[], turnActive = false) => host(
      <TimelineView items={currentItems} threadKey="42" scrollMemoryKey={`${bfcacheId}:agent:42`}
        reloadRestore={{ state: "enabled", session: browser.session, now: () => nowMs }} turnActive={turnActive} />,
      () => nowMs,
    );

    // meiva:note
    // A reload is an unmount and a mount under a fresh page entry.
    const firstVisit = renderWithBrowser(mountReloadView("tab-load-1", locatedItems), { browser });
    resize();
    readerScroll(1015);
    const savedMidOffset = readingOffset("5.0")!;
    firstVisit.unmount();

    top = 0;
    nowMs = 20_000;
    frames = [];
    const midReload = renderWithBrowser(mountReloadView("tab-load-2", locatedItems), { browser });
    expect(viewport().scrollTop).toBe(1015);
    expect(readingOffset("5.0")).toBe(savedMidOffset);
    expect(shownTops()).toEqual([1015]);
    expect(screen.queryByRole("alert")).toBeNull();

    readerScroll(4100);
    const savedNearTailOffset = readingOffset("14.0")!;
    midReload.unmount();

    top = 0;
    nowMs = 30_000;
    const nearTailReload = renderWithBrowser(mountReloadView("tab-load-3", locatedItems, true), { browser });
    resize();
    expect(viewport().scrollTop).toBe(4100);
    expect(readingOffset("14.0")).toBe(savedNearTailOffset);

    const grownItems: BackendTimelineItem[] = [
      ...locatedItems,
      { item_id: "17.0", kind: "agent_chat", payload: "New live reply below restored tail", source: null, created_at: "2026-11-18T12:00:00Z", inbound_id: null, show_timestamp: true },
      { item_id: "18.0", kind: "agent_reasoning", payload: "Analyzing live stream...", source: null, created_at: "2026-11-18T12:00:01Z", inbound_id: null, show_timestamp: false, reasoningStartedAt: nowMs - 1000 },
    ];
    act(() => {
      height = 5600;
      nearTailReload.rerender(mountReloadView("tab-load-3", grownItems, true));
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(viewport().scrollTop).toBe(4100);
    expect(readingOffset("14.0")).toBe(savedNearTailOffset);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Working");
    expect(screen.getByText("New live reply below restored tail")).toBeTruthy();
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();

    // meiva:note
    // Returning to the bottom clears the reload bookmark, so the next reload opens on the tail.
    readerScroll(5000);
    await nextFrame();
    expect(readReloadBookmark(browser.session, "42", nowMs)).toBeNull();
    nearTailReload.unmount();
    expect(readReloadBookmark(browser.session, "42", nowMs)).toBeNull();

    top = 0;
    nowMs = 40_000;
    const bottomReload = renderWithBrowser(mountReloadView("tab-load-4", grownItems, true), { browser });
    resize();
    expect(viewport().scrollTop).toBe(5000);
    act(() => {
      height = 6000;
      bottomReload.rerender(mountReloadView("tab-load-4", grownItems, true));
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(viewport().scrollTop).toBe(5400);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("finds a deep bookmark through background older pages while the rows stay hidden", () => {
    const browser = fakeBrowser({ session: {} });
    const allItems: BackendTimelineItem[] = Array.from({ length: 16 }, (_, idx) => ({
      item_id: `${idx + 1}.0`, kind: "agent_chat", payload: `History row ${idx + 1}`,
      source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: `loc-${idx + 1}`,
    }));
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-2", itemId: "2.0", rank: 0, viewportTop: -60, scrollTop: 360, savedAt: 5000 }, 5000);

    const origins: string[] = [];
    let sliceStart = 12;
    let loadingOlder = false;
    const onLoadOlder = vi.fn((origin: string) => {
      origins.push(origin);
      loadingOlder = true;
    });
    const renderSlice = () => host(
      <TimelineView items={allItems.slice(sliceStart)} threadKey="42" scrollMemoryKey="reload-older-pages:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        hasMoreOlder={sliceStart > 0} loadingOlder={loadingOlder} readerLoadingOlder={false} onLoadOlder={onLoadOlder} />,
    );
    const arrive = (start: number) => act(() => {
      sliceStart = start;
      loadingOlder = false;
      height = (16 - start) * 300 + 200;
      view.rerender(renderSlice());
    });

    height = (16 - sliceStart) * 300 + 200;
    const view = renderWithBrowser(renderSlice(), { browser });
    expect(origins).toEqual(["background"]);
    expect(rowsShown()).toBe(false);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("idle");

    arrive(8);
    expect(origins).toEqual(["background", "background"]);
    expect(rowsShown()).toBe(false);
    arrive(4);
    expect(origins).toEqual(["background", "background", "background"]);
    expect(rowsShown()).toBe(false);
    arrive(0);
    expect(rowsShown()).toBe(true);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(readingOffset("2.0")).toBe(-60);
    expect(viewport().scrollTop).toBe(360);
    expect(shownTops()).toEqual([360]);
  });

  it("opens the tail without error UI and drops a bookmark the history no longer holds", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-compacted-away", itemId: "3.0", rank: 0, viewportTop: 0, scrollTop: 900, savedAt: 5000 }, 5000);
    const pageItems = (startId: number): BackendTimelineItem[] => Array.from({ length: 4 }, (_, idx) => ({
      item_id: `${startId + idx}.0`, kind: "agent_chat", payload: `Row ${startId + idx}`,
      source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: `loc-current-${startId + idx}`,
    }));
    let currentItems = pageItems(50);
    let hasMoreOlder = true;
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => { loadingOlder = true; });
    const renderBounded = () => host(
      <TimelineView items={currentItems} threadKey="42" scrollMemoryKey="reload-stale:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        hasMoreOlder={hasMoreOlder} loadingOlder={loadingOlder} onLoadOlder={onLoadOlder} />,
    );

    height = 1400;
    const view = renderWithBrowser(renderBounded(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    act(() => {
      currentItems = [...pageItems(46), ...currentItems];
      loadingOlder = false;
      height = 2600;
      view.rerender(renderBounded());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    expect(rowsShown()).toBe(false);
    // meiva:note
    // The last page reaches the start of the history.
    act(() => {
      currentItems = [...pageItems(42), ...currentItems];
      hasMoreOlder = false;
      loadingOlder = false;
      height = 3800;
      view.rerender(renderBounded());
    });
    resize();

    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    expect(rowsShown()).toBe(true);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(viewport().scrollTop).toBe(3200);
    expect(readReloadBookmark(browser.session, "42", 6000)).toBeNull();
  });

  it("is not pushed into extra page requests by live rows arriving at the tail during the search", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-older-target", itemId: "2.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: 5000 }, 5000);
    const makeRow = (id: string, loc: string): BackendTimelineItem => ({
      item_id: id, kind: "agent_chat", payload: `Row ${id}`, source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: loc,
    });
    let currentItems: BackendTimelineItem[] = [makeRow("10.0", "loc-10"), makeRow("11.0", "loc-11")];
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => { loadingOlder = true; });
    const renderLiveBackfill = () => host(
      <TimelineView items={currentItems} threadKey="42" scrollMemoryKey="reload-live-tail-sse:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        turnActive hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={onLoadOlder} />,
    );

    const view = renderWithBrowser(renderLiveBackfill(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    act(() => {
      currentItems = [...currentItems, ...Array.from({ length: 12 }, (_, idx) => makeRow(`${12 + idx}.0`, `loc-live-${12 + idx}`))];
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    act(() => {
      currentItems = [makeRow("7.0", "loc-7"), makeRow("8.0", "loc-8"), makeRow("9.0", "loc-9"), ...currentItems];
      loadingOlder = false;
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);

    act(() => {
      currentItems = [makeRow("2.0", "loc-older-target"), makeRow("6.0", "loc-6"), ...currentItems];
      loadingOlder = false;
      height = currentItems.length * 300;
      view.rerender(renderLiveBackfill());
    });
    resize();
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(readingOffset("2.0")).toBe(0);
  });

  it("requests a failed page only once and falls back to the tail while live rows arrive", () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-unreachable-on-503", itemId: "2.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: 5000 }, 5000);
    const makeRow = (id: string, loc: string): BackendTimelineItem => ({
      item_id: id, kind: "agent_chat", payload: `Row ${id}`, source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: loc,
    });
    let currentItems: BackendTimelineItem[] = [makeRow("10.0", "loc-10"), makeRow("11.0", "loc-11")];
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => { loadingOlder = true; });
    const renderFailingBackfill = () => host(
      <TimelineView items={currentItems} threadKey="42" scrollMemoryKey="reload-fail-with-sse:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        turnActive hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={onLoadOlder} />,
    );

    height = 1100;
    const view = renderWithBrowser(renderFailingBackfill(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    // meiva:note
    // The page starts, then fails (HTTP 503) in the same commit a live row lands at the tail.
    act(() => { view.rerender(renderFailingBackfill()); });
    act(() => {
      loadingOlder = false;
      currentItems = [...currentItems, makeRow("12.0", "loc-live-12")];
      height = 1100;
      view.rerender(renderFailingBackfill());
    });
    resize();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(viewport().scrollTop).toBe(500);
    expect(readReloadBookmark(browser.session, "42", 6000)).toBeNull();
  });

  it("pages back from a post-compact window whose only cursor is the compact summary", () => {
    const postCompactHead: BackendTimelineItem[] = [
      { item_id: "0.0", kind: "system_prompt", payload: "System prompt", source: null, created_at: null, inbound_id: null, show_timestamp: false },
      { item_id: "1.0", kind: "inbound_compact_summary", payload: "Compacted summary", source: "compact", created_at: null, inbound_id: 1, show_timestamp: false, reading_locator: "loc-compact-summary" },
    ];
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-pre-compact-msg", itemId: "8.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: 5000 }, 5000);
    let currentItems = postCompactHead;
    let loadingOlder = false;
    const onLoadOlder = vi.fn(() => { loadingOlder = true; });
    const renderPostCompact = () => host(
      <TimelineView items={currentItems} threadKey="42" scrollMemoryKey="reload-post-compact:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={onLoadOlder} />,
    );

    const view = renderWithBrowser(renderPostCompact(), { browser });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    act(() => {
      currentItems = [
        { item_id: "s1.cp.7.0", kind: "agent_chat", payload: "Pre-compact paragraph", source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: "loc-pre-compact-msg" },
        ...postCompactHead,
      ];
      loadingOlder = false;
      view.rerender(renderPostCompact());
    });
    resize();
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(readingOffset("s1.cp.7.0")).toBe(0);
  });

  it("keeps no bookmark while following the tail across streaming, and reopens the tail without older reads", async () => {
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-stale", itemId: "2.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: 5000 }, 5000);
    const makeItems = (count: number): BackendTimelineItem[] => Array.from({ length: count }, (_, idx) => ({
      item_id: `${idx + 1}.0`, kind: "agent_chat", payload: `Stream row ${idx + 1}`,
      source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: `loc-${idx + 1}`,
    }));
    const renderView = (bfcacheId: string, list: BackendTimelineItem[], hasMoreOlder = false, onLoadOlder?: () => void) => host(
      <TimelineView items={list} threadKey="42" scrollMemoryKey={`${bfcacheId}:agent:42`}
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }}
        hasMoreOlder={hasMoreOlder} onLoadOlder={onLoadOlder} />,
    );

    let currentItems = makeItems(16);
    const view = renderWithBrowser(renderView("tail-stream-1", currentItems), { browser });
    expect(viewport().scrollTop).toBe(300);
    readerScroll(4400);
    await nextFrame();
    expect(readReloadBookmark(browser.session, "42", 6000)).toBeNull();

    currentItems = makeItems(60);
    act(() => {
      height = 18000;
      view.rerender(renderView("tail-stream-1", currentItems));
      resizeCallbacks.forEach((deliver) => deliver());
    });
    expect(viewport().scrollTop).toBe(17400);
    await nextFrame();
    expect(readReloadBookmark(browser.session, "42", 6000)).toBeNull();
    view.unmount();
    expect(readReloadBookmark(browser.session, "42", 6000)).toBeNull();

    top = 0;
    const onLoadOlder = vi.fn();
    renderWithBrowser(renderView("tail-stream-2", currentItems.slice(10), true, onLoadOlder), { browser });
    resize();
    expect(onLoadOlder).not.toHaveBeenCalled();
    expect(viewport().scrollTop).toBe(17400);
  });

  it("ends a stalled search at its time bound, cancels the page, and unblocks the reader's pull", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-stalled", itemId: "1.0", rank: 0, viewportTop: 0, scrollTop: 300, savedAt: Date.now() }, Date.now());
    const tailItems: BackendTimelineItem[] = items.slice(8).map((it, idx) => ({ ...it, reading_locator: `loc-tail-${idx}` }));
    const origins: string[] = [];
    let loadingOlder = false;
    const onLoadOlder = vi.fn((origin: string) => {
      origins.push(origin);
      loadingOlder = true;
    });
    const onCancelBackgroundLoadOlder = vi.fn(() => {
      if (!loadingOlder) return false;
      loadingOlder = false;
      return true;
    });
    const renderTimed = () => host(
      <TimelineView items={tailItems} threadKey="42" scrollMemoryKey="reload-timeout:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => Date.now() }}
        hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={onLoadOlder} onCancelBackgroundLoadOlder={onCancelBackgroundLoadOlder} />,
      () => Date.now(),
    );

    height = 2600;
    const view = renderWithBrowser(renderTimed(), { browser });
    act(() => { view.rerender(renderTimed()); });
    expect(origins).toEqual(["background"]);
    expect(rowsShown()).toBe(false);

    act(() => { vi.advanceTimersByTime(RELOAD_RESTORE_SEARCH_TIMEOUT_MS - 1); });
    expect(rowsShown()).toBe(false);
    act(() => { vi.advanceTimersByTime(1); });
    act(() => { view.rerender(renderTimed()); });

    expect(onCancelBackgroundLoadOlder).toHaveBeenCalledTimes(1);
    expect(rowsShown()).toBe(true);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(viewport().scrollTop).toBe(2000);
    expect(readReloadBookmark(browser.session, "42", Date.now())).toBeNull();

    act(() => {
      viewport().scrollTop = 0;
      fireEvent.wheel(viewport(), { deltaY: -100 });
    });
    expect(origins).toEqual(["background", "reader"]);
  });
});

describe("virtualized restore", () => {
  function setupVirtualizedLayout(tallItemId = "90.0", tallHeight = 1800) {
    const observers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = [];
    vi.stubGlobal("ResizeObserver", class {
      readonly record: (typeof observers)[number];
      constructor(callback: ResizeObserverCallback) {
        this.record = { callback, targets: new Set() };
        observers.push(this.record);
      }
      observe(target: Element) { this.record.targets.add(target); }
      unobserve(target: Element) { this.record.targets.delete(target); }
      disconnect() { this.record.targets.clear(); }
    });

    const rowHeight = (group: Element) => {
      const id = group.querySelector<HTMLElement>(".timeline-item")?.dataset.itemId;
      return id === tallItemId ? tallHeight : 80;
    };
    const content = () => screen.queryByTestId("history-viewport")?.querySelector<HTMLElement>('[role="log"]') ?? null;
    const contentHeight = () => {
      const log = content();
      if (!log) return 600;
      let total = 52;
      for (const child of log.children) {
        if (child.hasAttribute("data-virtual-group")) total += rowHeight(child) + 12;
        else if (child.hasAttribute("data-timeline-spacer")) total += Number.parseFloat((child as HTMLElement).style.height) + 12;
      }
      return Math.max(600, total);
    };

    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      if (!this.querySelectorAll(".timeline-item").length) return 600;
      return contentHeight();
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const vp = screen.queryByTestId("history-viewport");
      const log = content();
      if (this === vp) return rect(0, 600);
      if (this === log) return rect(-top, contentHeight());
      if (this.hasAttribute("data-timeline-reserve")) return rect(contentHeight() - top, 0);
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      if (!group || !log) return rect(0, 0);
      let y = 52;
      for (const sibling of log.children) {
        if (sibling === group) break;
        if (sibling.hasAttribute("data-virtual-group")) y += rowHeight(sibling) + 12;
        else if (sibling.hasAttribute("data-timeline-spacer")) y += Number.parseFloat((sibling as HTMLElement).style.height) + 12;
      }
      return rect(y - top, rowHeight(group));
    });

    const flushObservers = () => {
      act(() => {
        for (const { callback, targets } of observers) {
          const entries = [...targets]
            .filter((t) => t.isConnected)
            .map((target) => ({
              target,
              borderBoxSize: [{ blockSize: target instanceof HTMLElement ? target.getBoundingClientRect().height : 0 }],
            })) as unknown as ResizeObserverEntry[];
          callback(entries, {} as ResizeObserver);
        }
      });
    };

    const virtualItems: BackendTimelineItem[] = Array.from({ length: 110 }, (_, idx) => ({
      item_id: `${idx + 1}.0`, kind: "agent_chat",
      payload: idx + 1 === 90 ? "Very long message ".repeat(40) : `Virtual message ${idx + 1}`,
      source: null, created_at: null, inbound_id: null, show_timestamp: false, reading_locator: `loc-virt-${idx + 1}`,
    }));
    return { virtualItems, flushObservers };
  }

  function scrollDownToBottom(flushObservers: () => void, withWheel: boolean) {
    for (let step = 0; step < 12; step += 1) {
      const maxTop = Math.max(0, viewport().scrollHeight - viewport().clientHeight);
      if (top >= maxTop) break;
      const nextTop = Math.min(maxTop, top + 450);
      act(() => {
        if (withWheel) fireEvent.wheel(viewport(), { deltaY: 450 });
        viewport().scrollTop = nextTop;
        fireEvent.scroll(viewport());
      });
      flushObservers();
    }
    const finalMax = Math.max(0, viewport().scrollHeight - viewport().clientHeight);
    act(() => {
      if (withWheel) fireEvent.wheel(viewport(), { deltaY: 450 });
      viewport().scrollTop = finalMax;
      fireEvent.scroll(viewport());
    });
    flushObservers();
  }

  const expectLatestRowsMounted = () => {
    expect(viewport().querySelector('[data-timeline-spacer="after"]')).toBeNull();
    for (const id of ["108.0", "109.0", "110.0"]) expect(viewport().querySelector(`.timeline-item[data-item-id="${id}"]`)).not.toBeNull();
    expect(viewport().scrollHeight - viewport().clientHeight - viewport().scrollTop).toBeLessThanOrEqual(1);
  };

  it("restores a memory place in a virtualized window and keeps the latest rows reachable below it", () => {
    const { virtualItems, flushObservers } = setupVirtualizedLayout();
    writeMemoryPlace("virt-inmemory-tail:agent:42", "42", place("85.0", 0));
    renderWithBrowser(host(<TimelineView items={virtualItems} threadKey="42" scrollMemoryKey="virt-inmemory-tail:agent:42" />));
    expect(viewport().querySelector<HTMLElement>('[role="log"]')!.className).toContain("timeline-virtual");
    expect(readingOffset("85.0")).toBe(0);
    flushObservers();
    expect(readingOffset("85.0")).toBe(0);
    scrollDownToBottom(flushObservers, true);
    expectLatestRowsMounted();
  });

  it.each([true, false])("restores a reload bookmark in a virtualized window and keeps the latest rows reachable (wheel: %s)", (withWheel) => {
    const { virtualItems, flushObservers } = setupVirtualizedLayout();
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-virt-85", itemId: "85.0", rank: 0, viewportTop: 0, scrollTop: 7780, savedAt: 5000 }, 5000);
    renderWithBrowser(host(
      <TimelineView items={virtualItems} threadKey="42" scrollMemoryKey={`virt-reload-tail-${String(withWheel)}:agent:42`}
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }} />,
    ), { browser });
    expect(viewport().querySelector<HTMLElement>('[role="log"]')!.className).toContain("timeline-virtual");
    flushObservers();
    expect(readingOffset("85.0")).toBe(0);
    scrollDownToBottom(flushObservers, withWheel);
    expectLatestRowsMounted();
  });

  it("reaches the latest rows with the End key from a restored virtualized place", () => {
    const { virtualItems, flushObservers } = setupVirtualizedLayout();
    const browser = fakeBrowser({ session: {} });
    saveReloadBookmark(browser.session, "42", { readingLocator: "loc-virt-85", itemId: "85.0", rank: 0, viewportTop: 0, scrollTop: 7780, savedAt: 5000 }, 5000);
    renderWithBrowser(host(
      <TimelineView items={virtualItems} threadKey="42" scrollMemoryKey="virt-keydown-tail:agent:42"
        reloadRestore={{ state: "enabled", session: browser.session, now: () => 6000 }} />,
    ), { browser });
    flushObservers();
    expect(readingOffset("85.0")).toBe(0);
    act(() => {
      fireEvent.keyDown(viewport(), { key: "End" });
      viewport().scrollTop = Math.max(0, viewport().scrollHeight - viewport().clientHeight);
      fireEvent.scroll(viewport());
    });
    flushObservers();
    expect(viewport().querySelector('[data-timeline-spacer="after"]')).toBeNull();
    expect(viewport().querySelector('.timeline-item[data-item-id="110.0"]')).not.toBeNull();
  });

  it("does not jitter back to tail on a small mobile touch scroll-up in a virtualized timeline", () => {
    const { virtualItems, flushObservers } = setupVirtualizedLayout();
    const browser = fakeBrowser({ media: { "(pointer: coarse)": true } });
    renderWithBrowser(host(
      <TimelineView items={virtualItems} threadKey="42" scrollMemoryKey="virt-mobile-up:agent:42" />,
    ), { browser });
    flushObservers();
    const tail = Math.max(0, viewport().scrollHeight - viewport().clientHeight);
    expect(viewport().scrollTop).toBe(tail);

    act(() => {
      fireEvent.touchStart(viewport(), { touches: [{ clientX: 100, clientY: 500 }] });
      fireEvent.touchMove(viewport(), { touches: [{ clientX: 100, clientY: 505 }] });
      viewport().scrollTop = tail - 5;
      fireEvent.scroll(viewport());
      fireEvent.touchEnd(viewport(), { touches: [] });
    });
    flushObservers();
    expect(viewport().scrollTop).toBe(tail - 5);

    for (const nextTop of [tail - 8, tail - 10, tail - 10.4]) {
      act(() => {
        viewport().scrollTop = nextTop;
        fireEvent.scroll(viewport());
      });
      flushObservers();
      expect(viewport().scrollTop).toBe(nextTop);
    }
  });
});
