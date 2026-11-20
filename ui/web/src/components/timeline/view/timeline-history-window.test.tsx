import { act, fireEvent, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, displayLimits, makeItem, pullOlderAtTop, render, timelineLayout } from "./timeline-test-mock";

vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { findRowNode } from "../anchor/dom";
import { ItemView } from "../item";
import { useTimelineWindow } from "../model/use-timeline-window";

afterEach(cleanupTimelineTest);

const chat = (n: number) => makeItem({ item_id: `${n}.0`, kind: "agent_chat", payload: `Reply ${n}` });
const chats = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, index) => chat(from + index));

// meiva:note
// A reader scrolls in steps no longer than the viewport (wheel, keys), so the window has mounted
// the rows under each new position before the next one.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.md
function stepTo(layout: ReturnType<typeof timelineLayout>, target: number) {
  const step = 500;
  while (Math.abs(layout.top() - target) > step) {
    layout.scrollTo(layout.top() + (target > layout.top() ? step : -step));
  }
  layout.scrollTo(target);
}

describe("deep history DOM window", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  // meiva:note
  // A reader parked in a long history (row n at 52 + (n-1)*92 in scroll space), with `count` rows
  // loaded: 90 at first, the rest streamed in below while they read.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.md
  function trackedHistory(count: number) {
    const layout = timelineLayout();
    const items = chats(1, count);
    const loadOlder = vi.fn();
    const view = (rows: BackendTimelineItem[], loadingOlder = false) => (
      <TimelineView items={rows} threadKey="tracked-bottom" hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={loadOlder} />
    );
    const { rerender } = render(view(items.slice(0, 90)));
    layout.resize();
    stepTo(layout, 3000);
    rerender(view(items));
    expect(layout.top()).toBe(3000);
    // meiva:note
    // Away from the bottom the control is present and interactive; at the bottom
    // it is not rendered at all (user report 2026-10-30).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    expect(screen.getByLabelText("Scroll down").className).not.toContain("opacity-0");
    if (count > 100) expect(layout.viewport().querySelector("[data-timeline-spacer]")).not.toBeNull();
    layout.writes.length = 0;
    return {
      layout, items, loadOlder,
      rerender: (rows: BackendTimelineItem[], loadingOlder = false) => rerender(view(rows, loadingOlder)),
      remount: (threadKey: string) => rerender(
        <TimelineView key={threadKey} items={items} threadKey={threadKey} hasMoreOlder onLoadOlder={loadOlder} />,
      ),
    };
  }

  it.each([90, 300])("lets the bottom button finish its smooth scroll with %i tracked rows", (count) => {
    const { layout } = trackedHistory(count);
    const end = layout.naturalEnd();
    fireEvent.click(screen.getByLabelText("Scroll down"));
    expect(layout.smoothTarget()).toBe(end);
    expect(layout.writes).toEqual([]);
    layout.finishSmooth();
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.top()).toBe(layout.max());
    expect(layout.viewport().querySelector(`.timeline-item[data-item-id="${count}.0"]`)).not.toBeNull();
    expect(screen.queryByLabelText("Scroll down")).toBeNull();
  });

  // meiva:note
  // Each agent mounts its own view (Home keys TimelineView by agent), so a switch is a fresh mount:
  // a first visit with no saved place opens on the tail.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  it.each([90, 300])("opens a switched-to thread on the tail with %i tracked rows", (count) => {
    const { layout, remount } = trackedHistory(count);
    act(() => { remount("next-thread"); });
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.top()).toBe(layout.max());
    expect(layout.viewport().querySelector(`.timeline-item[data-item-id="${count}.0"]`)).not.toBeNull();
  });

  it("stays on the tail when an older page lands after the bottom button's ride", () => {
    const { layout, items, loadOlder, rerender } = trackedHistory(200);
    stepTo(layout, 0);
    pullOlderAtTop(layout.viewport());
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(items, true);
    fireEvent.click(screen.getByLabelText("Scroll down"));
    layout.finishSmooth();
    expect(layout.top()).toBe(layout.naturalEnd());
    const older = Array.from({ length: 20 }, (_, index) => makeItem({
      item_id: `s1.older.${index + 1}.0`, kind: "agent_chat", payload: `Older ${index + 1}`,
    }));
    rerender([...older, ...items]);
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.viewport().querySelector('.timeline-item[data-item-id="200.0"]')).not.toBeNull();
  });

  it("uses configured activation and child-window row thresholds", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    const chats = Array.from({ length: 60 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    const { unmount } = render(<TimelineView items={chats} threadKey="configured-activation" />);
    let viewport = screen.getByTestId("scroll-viewport");
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="before"]')).not.toBeNull();
    unmount();

    displayLimits.set("MEIVA_TIMELINE_WINDOW_TURN_ROWS", 20);
    const reasoning = Array.from({ length: 60 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_reasoning", payload: `Thought ${index + 1}`,
    }));
    render(<TimelineView items={reasoning} threadKey="configured-turn" />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    viewport = screen.getByTestId("scroll-viewport");
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
  });

  it("windows expanded historical details through the outer timeline without an inner scrollport", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_TURN_ROWS", 20);
    const reasoning = Array.from({ length: 60 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_reasoning", payload: `Thought ${index + 1}`,
    }));
    render(<TimelineView items={[...reasoning, makeItem({ item_id: "61.0", kind: "agent_chat", payload: "Done" })]} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const viewport = screen.getByTestId("scroll-viewport");
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    expect(viewport.querySelectorAll("[data-virtual-row]").length).toBeLessThan(60);
    expect(viewport.querySelectorAll("[data-virtual-row]").length).toBeGreaterThan(0);
    expect(viewport.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
    expect(screen.getByText("Done")).toBeTruthy();
  });

  it("mounts the pinned child at its own rank when compact ranks share an item ID", () => {
    const rows = Array.from({ length: 60 }, (_, i) => i === 0 ? "turn:buffer:0:shared"
      : i === 50 ? "turn:canonical:1:shared" : `turn:canonical:1:${i}.0`);
    const { result } = renderHook(() => useTimelineWindow({
      groups: [{ key: "turn", rank: 0, itemIds: ["shared"], memberRankKeys: ["0:shared", "1:shared"], estimatedHeight: 6000, expandedRows: rows }],
      enabled: true, turnRows: 20, measureRows: 10, identity: "ranked-history",
      viewportRef: { current: null }, contentRef: { current: null },
      isFollowing: () => false, pin: { itemId: "shared", rank: 1 }, tail: false,
    }));
    const range = result.current.rowRange(0);
    expect(range.start).toBe(38);
    expect(range.end).toBe(60);
  });

  it("keeps the clicked work header in place when it expands above a primary message", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 5);
    const layout = timelineLayout({ rowHeight: 600 });
    const items = [
      ...Array.from({ length: 8 }, (_, index) => makeItem({
        item_id: `1.${index}`, kind: "agent_reasoning", payload: `Step ${index + 1}`,
      })),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "Final reply" }),
    ];
    render(<TimelineView items={items} threadKey="expand-keep-top" />);
    layout.scrollTo(40);
    expect(layout.top()).toBe(40);
    const headerTop = layout.rowTop("1.0");
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    expect(layout.top()).toBe(40);
    expect(layout.rowTop("1.0")).toBe(headerTop);
    layout.resize();
    expect(layout.rowTop("1.0")).toBe(headerTop);
  });

  it("does not pin scrollTop to bottom when expanding a settled work block while following at the bottom (#6581)", () => {
    const observers: ResizeObserverCallback[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { observers.push(cb); }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    });
    const items = [
      ...Array.from({ length: 12 }, (_, index) => makeItem({
        item_id: `1.${index}`, kind: "agent_reasoning", payload: `Step ${index + 1}`,
      })),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "Final reply" }),
    ];
    render(<TimelineView items={items} threadKey="expand-at-bottom-no-pin" />);
    const viewport = screen.getByTestId("scroll-viewport");
    let scrollHeight = 1200;
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "scrollHeight", { get: () => scrollHeight, configurable: true });
    act(() => {
      viewport.scrollTop = 600;
      fireEvent.scroll(viewport);
    });
    expect(viewport.scrollTop).toBe(600);

    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    scrollHeight = 2000;
    act(() => {
      for (const cb of observers) cb([], {} as ResizeObserver);
    });
    expect(viewport.scrollTop).toBe(600);
    vi.unstubAllGlobals();
  });

  it("pre-tracks group heights when collapsed turns hold enough items to cross activation on expand (#6581)", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 30);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 20);
    const observed: Element[] = [];
    vi.stubGlobal("ResizeObserver", class {
      observe(target: Element) { observed.push(target); }
      unobserve = vi.fn();
      disconnect = vi.fn();
    });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "Opening reply" }),
      ...Array.from({ length: 35 }, (_, index) => makeItem({
        item_id: `2.${index}`, kind: "agent_reasoning", payload: `Step ${index + 1}`,
      })),
    ];
    render(<TimelineView items={items} threadKey="pretrack-collapsed-turn" />);
    expect(observed.some((node) => node.hasAttribute("data-virtual-group"))).toBe(true);
    vi.unstubAllGlobals();
  });

  // meiva:note
  // SUSPECTED PRODUCT BUG (reported, not fixed here): with the window active, an older page that
  // lands while the reader is parked in history leaves the window on the old scrollTop, which now
  // holds the new oldest rows; the reader's anchor row is unmounted, nothing pins it (`pin` only
  // covers placements, and `lost` ignores a row that is still loaded), so the writer cannot hold it.
  // Measured: rows 31-45 -> 1-15 at the same scrollTop 200; the same with 120 rows at the default
  // activation and a pull at the top (row 51 at 52 -> rows from 1.0 at 52).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  it("keeps the reader's row and never jumps to the bottom when older history prepends after scrolling up", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 5);
    const layout = timelineLayout();
    const allItems = chats(1, 60);
    const tailItems = allItems.slice(30);
    const { rerender } = render(
      <TimelineView items={tailItems} threadKey="scroll-up-prepend" hasMoreOlder loadingOlder={false} />,
    );
    layout.resize();
    stepTo(layout, 200);
    const screenTopBefore = layout.rowTop("32.0");
    rerender(<TimelineView items={tailItems} threadKey="scroll-up-prepend" hasMoreOlder loadingOlder />);
    rerender(<TimelineView items={allItems} threadKey="scroll-up-prepend" hasMoreOlder={false} loadingOlder={false} />);
    expect(layout.rowTop("32.0")).toBe(screenTopBefore);
    expect(layout.top()).toBeGreaterThan(200);
    expect(layout.top()).toBeLessThan(layout.naturalEnd() - 600);
    layout.resize();
    expect(layout.rowTop("32.0")).toBe(screenTopBefore);
  });

  it("starts measuring at the configured row count before activation", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 50);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 10);
    const observed: Element[] = [];
    vi.stubGlobal("ResizeObserver", class {
      observe(target: Element) { observed.push(target); }
      unobserve = vi.fn();
      disconnect = vi.fn();
    });
    const chats = Array.from({ length: 20 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    render(<TimelineView items={chats} threadKey="configured-measure" />);
    expect(observed.some((node) => node.hasAttribute("data-virtual-group"))).toBe(true);
    vi.unstubAllGlobals();
  });

  it("measures the mounted rows before a late config read lowers activation", () => {
    const observed: Element[] = [];
    vi.stubGlobal("ResizeObserver", class {
      observe(target: Element) { observed.push(target); }
      unobserve = vi.fn();
      disconnect = vi.fn();
    });
    const chats = Array.from({ length: 60 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    const { rerender } = render(<TimelineView items={chats} threadKey="late-config" />);
    const viewport = screen.getByTestId("scroll-viewport");
    expect(viewport.querySelectorAll(".timeline-item").length).toBe(60);

    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    rerender(<TimelineView items={chats} threadKey="late-config" />);
    expect(observed.filter((node) => node.hasAttribute("data-virtual-group")).length).toBeGreaterThanOrEqual(60);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
  });

  it("keeps keyboard focus in the log when a focused card leaves the window", () => {
    const items = Array.from({ length: 1200 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    render(<TimelineView items={items} threadKey="focus-window" />);
    const viewport = screen.getByTestId("scroll-viewport");
    const content = viewport.querySelector<HTMLElement>('[role="log"]')!;
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600, height: 600 } as DOMRect;
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      const id = group?.querySelector<HTMLElement>(".timeline-item")?.dataset.itemId;
      const top = id ? 52 + (Number.parseInt(id) - 1) * 92 - viewport.scrollTop : 0;
      return { top, bottom: top + 80, height: 80 } as DOMRect;
    });
    const button = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="1200.0"] button')!;
    button.focus();
    expect(document.activeElement).toBe(button);
    act(() => { viewport.scrollTop = 0; fireEvent.scroll(viewport); });
    expect(document.activeElement).toBe(content);
    vi.restoreAllMocks();
  });

  it("resolves a child row through its collapsed turn and rejects the same ID at another rank", () => {
    const viewport = document.createElement("div");
    const turn = document.createElement("div");
    turn.dataset.turnId = "3.0";
    turn.dataset.turnExpanded = "false";
    turn.dataset.turnMemberIds = "3.0 4.0";
    turn.dataset.turnMemberRanks = "0:3.0 0:4.0";
    turn.dataset.itemId = "3.0";
    turn.dataset.displayRank = "0";
    viewport.append(turn);
    expect(findRowNode(viewport, { itemId: "4.0", rank: 0 })).toEqual({ node: turn, collapsed: true });
    expect(findRowNode(viewport, { itemId: "3.0", rank: 0 })).toEqual({ node: turn, collapsed: true });
    expect(findRowNode(viewport, { itemId: "3.0", rank: 0, turn: true })).toEqual({ node: turn, collapsed: false });
    expect(findRowNode(viewport, { itemId: "4.0", rank: 1 })).toBeNull();
  });

  it("does not transfer an old thread's reading place onto identical IDs in a new thread", () => {
    const layout = timelineLayout();
    const items = chats(1, 300);
    const view = (threadKey: string) => (
      <TimelineView key={threadKey} items={items} threadKey={threadKey} scrollMemoryKey="entry:shared" />
    );
    const { rerender } = render(view("old-thread"));
    layout.resize();
    stepTo(layout, 20_000);
    const reading = layout.viewport().querySelector<HTMLElement>(".timeline-item")!;
    expect(reading).not.toBeNull();
    // meiva:note
    // Returning to the same thread lands on the saved row.
    act(() => { rerender(view("old-thread-away")); });
    act(() => { rerender(view("old-thread")); });
    expect(layout.top()).toBe(20_000);
    act(() => { rerender(view("new-thread")); });
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.top()).toBe(layout.max());
  });

  it("keeps the exact reading row while crossing 100 to 101 variable-height rows", () => {
    const layout = timelineLayout({ rowHeight: (row) => (Number.parseInt(row.dataset.itemId ?? "0") % 3 === 0 ? 260 : 60) });
    const items = chats(1, 101);
    const { rerender } = render(<TimelineView items={items.slice(0, 100)} threadKey="threshold" />);
    layout.resize();
    expect(layout.top()).toBe(layout.naturalEnd());
    stepTo(layout, layout.rowTop("51.0") + layout.top() - 100);
    const top = layout.rowTop("51.0");
    expect(top).toBe(100);
    rerender(<TimelineView items={items} threadKey="threshold" />);
    expect(layout.viewport().querySelector("[data-timeline-spacer]")).not.toBeNull();
    expect(layout.rowTop("51.0")).toBe(top);
    layout.resize();
    expect(layout.rowTop("51.0")).toBe(top);
    expect(layout.viewport().querySelectorAll(".timeline-item").length).toBeLessThan(80);
  });

  it("subscribes to scroll and resize once while streamed updates arrive", () => {
    const observers: { targets: Set<Element>; observed: Element[] }[] = [];
    vi.stubGlobal("ResizeObserver", class {
      readonly record = { targets: new Set<Element>(), observed: [] as Element[] };
      constructor() { observers.push(this.record); }
      observe(target: Element) { this.record.targets.add(target); this.record.observed.push(target); }
      unobserve(target: Element) { this.record.targets.delete(target); }
      disconnect() { this.record.targets.clear(); }
    });
    const chat = (n: number) => makeItem({ item_id: `${n}.0`, kind: "agent_chat", payload: `Reply ${n}` });
    let items = Array.from({ length: 120 }, (_, index) => chat(index + 1));
    const { rerender } = render(<TimelineView items={items} threadKey="streaming-window" />);
    const viewport = screen.getByTestId("scroll-viewport");
    const addListener = vi.spyOn(viewport, "addEventListener");
    const constructed = observers.length;
    const rowObserver = observers.find(({ targets }) =>
      [...targets].some((node) => node.hasAttribute("data-virtual-group")));
    expect(rowObserver).toBeDefined();
    const observedBefore = rowObserver!.observed.length;

    for (let n = 121; n <= 125; n++) {
      items = [...items, chat(n)];
      rerender(<TimelineView items={items} threadKey="streaming-window" />);
    }

    expect(addListener.mock.calls.filter(([type]) => type === "scroll")).toEqual([]);
    expect(observers.length).toBe(constructed);
    expect(rowObserver!.observed.length - observedBefore).toBeLessThanOrEqual(5);
    expect([...rowObserver!.targets].every((node) => node.isConnected)).toBe(true);
    vi.restoreAllMocks();
  });

  it("keeps the reader when a row above grows after layout", () => {
    let tallHeight = 80;
    const layout = timelineLayout({ rowHeight: (row) => (row.dataset.itemId === "50.0" ? tallHeight : 80) });
    render(<TimelineView items={chats(1, 300)} threadKey="large-row" />);
    layout.resize();
    stepTo(layout, 52 + 53 * 92 - 100);
    expect(layout.rowNode("50.0")).not.toBeNull();
    const top = layout.rowTop("54.0");
    tallHeight = 90_000;
    layout.resize();
    expect(layout.rowTop("54.0")).toBe(top);
    layout.resize();
    expect(layout.rowTop("54.0")).toBe(top);
    expect(layout.viewport().querySelectorAll(".timeline-item").length).toBeLessThan(80);
  });

  // meiva:note
  // SUSPECTED PRODUCT BUG (reported, not fixed here): before the rows' ResizeObserver reports, the
  // window positions rows at its 80px estimate. With the tail anchored, every commit the writer
  // pins the natural end (real heights), the window re-measures that position in estimated space
  // and swaps the mounted rows, which moves the natural end again: a synchronous layout-effect
  // loop (measured: writes alternate 10916 <-> 11236 for 120 rows of 160px) until React throws
  // "Maximum update depth exceeded". 80-100px rows converge; 160px and taller do not.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.md
  it("opens a long tail of rows taller than the window's estimate on the tail", () => {
    const layout = timelineLayout({ rowHeight: 160 });
    render(<TimelineView items={chats(1, 120)} threadKey="tall-tail" />);
    expect(layout.top()).toBe(layout.naturalEnd());
    layout.resize();
    expect(layout.top()).toBe(layout.naturalEnd());
  });

  it("mounts only the viewport and nearby rows while retaining the full scroll space", async () => {
    const layout = timelineLayout({ viewportHeight: 720 });
    render(<TimelineView items={chats(1, 1200)} threadKey="deep-history" />);
    layout.resize();
    stepTo(layout, 48_000);
    expect(layout.top()).toBe(48_000);
    await waitFor(() => {
      expect(layout.viewport().querySelectorAll(".timeline-item").length).toBeLessThan(60);
      expect(layout.viewport().querySelector('[data-timeline-spacer="before"]')).not.toBeNull();
      expect(layout.viewport().querySelector('[data-timeline-spacer="after"]')).not.toBeNull();
    });
  });

  // meiva:note
  // SUSPECTED PRODUCT BUG (reported, not fixed here): a reader scroll that lands where no row is
  // mounted yet (scrollbar drag, Home, find-in-page into a virtual spacer) reads no row back, so the
  // previous anchor stays; the commit that mounts rows there then writes the old anchor back and
  // the reader is thrown to the tail (measured: 48000 -> 109876 on 1200 rows).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  it("keeps a jump into unmounted history where the reader put it", () => {
    const layout = timelineLayout({ viewportHeight: 720 });
    render(<TimelineView items={chats(1, 1200)} threadKey="deep-jump" />);
    layout.resize();
    layout.scrollTo(48_000);
    expect(layout.top()).toBe(48_000);
    expect(layout.viewport().querySelector('[data-timeline-spacer="after"]')).not.toBeNull();
  });

  // meiva:note
  // An expanded work block has no scrollport of its own: its rows are windowed
  // by the timeline's one window, so scrolling the timeline is what moves the
  // mounted rows inside a long run (user ruling 2026-11-09).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  it("windows the children of one long expanded work block through the timeline's own window", () => {
    const layout = timelineLayout({ viewportHeight: 360 });
    const items = Array.from({ length: 600 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`,
      kind: "agent_reasoning",
      payload: `Thought ${index + 1}`,
    }));
    render(<TimelineView items={items} threadKey="deep-run" />);
    layout.resize();
    fireEvent.click(screen.getByTestId("turn-toggle"));
    layout.resize();
    const viewport = layout.viewport();
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    expect(viewport.querySelectorAll('[data-turn-expanded="true"]').length).toBe(1);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="turn-after"]')).not.toBeNull();
    stepTo(layout, 20_000);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
    expect(viewport.querySelector('[data-timeline-spacer="turn-after"]')).not.toBeNull();
  });
});


it("preserves every paragraph of a human message", () => {
  const item = makeItem({ kind: "inbound_chat", source: "user", payload: "User at 12:00:\n\nFirst paragraph.\n\nSecond paragraph." });
  const { container } = render(<ItemView item={item} streaming={false} />);
  expect(container.querySelector("[data-testid=chat-markdown]")?.textContent).toContain("First paragraph.");
  expect(container.textContent).toContain("Second paragraph.");
});


it("renders image-only human messages as thumbnails", () => {
  const item = makeItem({ kind: "inbound_chat", source: "user", payload: "[image]",
    images: ["/api/agents/42/uploads/screenshot.png"] });
  const { container } = render(<ItemView item={item} streaming={false} />);
  expect(container.querySelector("img")?.getAttribute("src")).toContain("/api/agents/42/uploads/screenshot.png");
  expect(container.textContent).not.toContain("[image]");
});


it("restores expanded work and detail blocks when returning to an agent", () => {
  const work = [makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "Read this detail" })];
  const { rerender } = render(<TimelineView key="read-A" threadKey="A" scrollMemoryKey="expansion:A" items={work} />);
  fireEvent.click(screen.getByTestId("turn-toggle"));
  fireEvent.click(screen.getByTestId("card-toggle"));
  expect(screen.getByText("Read this detail")).toBeTruthy();
  rerender(<TimelineView key="read-B" threadKey="B" scrollMemoryKey="expansion:B" items={[]} />);
  rerender(<TimelineView key="read-A" threadKey="A" scrollMemoryKey="expansion:A" items={work} />);
  expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByTestId("card-toggle").getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("Read this detail")).toBeTruthy();
});
