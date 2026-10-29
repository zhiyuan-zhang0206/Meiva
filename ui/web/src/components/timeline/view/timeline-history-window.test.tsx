import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, displayLimits, makeItem, pullOlderAtTop, render, setToggleState } from "./timeline-test-mock";

vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { ItemView } from "../item";
import { resolveSavedTimelineAnchor } from "../model/use-timeline-window";

afterEach(cleanupTimelineTest);
describe("deep history DOM window", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  function trackedHistory(count: number) {
    const items = Array.from({ length: count }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    const loadOlder = vi.fn();
    const view = (rows: BackendTimelineItem[], threadKey = "tracked-bottom") => (
      <TimelineView items={rows} threadKey={threadKey} hasMoreOlder onLoadOlder={loadOlder} />
    );
    const { rerender } = render(view(items.slice(0, 90)));
    const viewport = screen.getByTestId("scroll-viewport");
    let height = 52 + Math.min(count, 90) * 92;
    let top = height - 600;
    let smoothTarget: number | null = null;
    const writes: number[] = [];
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (value: number) => {
          smoothTarget = null;
          writes.push(value);
          top = Math.max(0, Math.min(value, height - 600));
        },
      },
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600, height: 600 } as DOMRect;
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      const id = group?.querySelector<HTMLElement>(".timeline-item")?.dataset.itemId;
      const rowTop = id ? 52 + (Number.parseInt(id) - 1) * 92 - top : 0;
      return { top: rowTop, bottom: rowTop + 80, height: 80 } as DOMRect;
    });
    act(() => { fireEvent.scroll(viewport); });
    act(() => { top = 3000; fireEvent.scroll(viewport); });
    height = 52 + count * 92;
    rerender(view(items));
    expect(viewport.scrollTop).toBe(3000);
    // meiva:note
    // Away from the bottom the control is present and interactive; at the bottom
    // it is not rendered at all (user report 2026-10-30).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
    expect(screen.getByLabelText("Scroll to bottom").className).not.toContain("opacity-0");
    if (count > 100) expect(viewport.querySelector("[data-timeline-spacer]")).not.toBeNull();
    const scrollTo = vi.spyOn(viewport, "scrollTo").mockImplementation((options?: ScrollToOptions | number) => {
      if (typeof options === "object") smoothTarget = options.top ?? null;
    });
    writes.length = 0;
    return {
      viewport, height, writes, scrollTo,
      switchThread: (threadKey: string) => rerender(view(items, threadKey)),
      smoothTarget: () => smoothTarget,
      finishSmoothScroll: () => {
        top = height - 600;
        rerender(view([...items]));
      },
      requestOlder: () => {
        for (let next = top - 300; next > 0; next -= 300) {
          act(() => { top = next; fireEvent.scroll(viewport); });
        }
        act(() => { top = 0; fireEvent.scroll(viewport); });
        pullOlderAtTop(viewport);
        expect(loadOlder).toHaveBeenCalledTimes(1);
      },
    };
  }

  it.each([90, 300])("lets the bottom button finish its smooth scroll with %i tracked rows", (count) => {
    const history = trackedHistory(count);
    fireEvent.click(screen.getByLabelText("Scroll to bottom"));
    expect(history.scrollTo).toHaveBeenCalledWith({ top: history.height, behavior: "smooth" });
    expect(history.writes).toEqual([]);
    expect(history.smoothTarget()).toBe(history.height);
    history.finishSmoothScroll();
    expect(history.viewport.scrollTop).toBe(history.height - 600);
    expect(history.viewport.querySelector(`.timeline-item[data-item-id="${count}.0"]`)).not.toBeNull();
  });

  it.each([90, 300])("pins a thread switch to the bottom with %i tracked rows", (count) => {
    const history = trackedHistory(count);
    act(() => { history.switchThread("next-thread"); });
    expect(history.viewport.scrollTop).toBe(history.height - 600);
    expect(history.viewport.querySelector(`.timeline-item[data-item-id="${count}.0"]`)).not.toBeNull();
  });

  it("releases a pending older-page anchor when the bottom button is clicked", () => {
    const history = trackedHistory(200);
    history.requestOlder();
    fireEvent.click(screen.getByLabelText("Scroll to bottom"));
    history.finishSmoothScroll();
    expect(history.viewport.scrollTop).toBe(history.height - 600);
    expect(history.viewport.querySelector('.timeline-item[data-item-id="200.0"]')).not.toBeNull();
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
    viewport = screen.getByTestId("scroll-viewport");
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
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

  it("restores a child through a collapsed turn and rejects the same ID in another rank", () => {
    const viewport = document.createElement("div");
    const turn = document.createElement("div");
    turn.dataset.turnExpanded = "false";
    turn.dataset.turnMemberIds = "3.0 4.0";
    turn.dataset.itemId = "3.0";
    turn.dataset.displayRank = "0";
    viewport.append(turn);
    const saved = { itemId: "4.0", rank: 0, viewportTop: -120 };
    const child = makeItem({ item_id: "4.0", kind: "agent_reasoning", payload: "child" });
    const collapsed = resolveSavedTimelineAnchor(viewport, saved, null, [child]);
    expect(collapsed.node).toBe(turn);
    expect(collapsed.present).toBe(true);
    expect(collapsed.viewportTop).toBe(52);

    turn.dataset.displayRank = "2";
    const buffer: CompactTransitionBuffer = {
      threadId: 1, epoch: 1, rows: [{ item: child, rank: 2, needsCanonicalRow: true }],
      oldestRealByRank: new Map([[2, "4.0"]]), tailReady: true,
    };
    const absent = resolveSavedTimelineAnchor(viewport, { ...saved, rank: 1 }, buffer, [child]);
    expect(absent.node).toBeNull();
    expect(absent.present).toBe(false);
  });

  it("does not transfer an old thread's reading pin onto identical IDs in a new thread", () => {
    const items = Array.from({ length: 1200 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    const { rerender } = render(<TimelineView items={items} threadKey="old-thread" />);
    const viewport = screen.getByTestId("scroll-viewport");
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 120_000, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600, height: 600 } as DOMRect;
      const row = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      const id = row?.querySelector<HTMLElement>(".timeline-item")?.dataset.itemId;
      const top = id ? 52 + (Number.parseInt(id) - 1) * 92 - viewport.scrollTop : 0;
      return { top, bottom: top + 80, height: 80 } as DOMRect;
    });
    act(() => {
      viewport.scrollTop = 40_000;
      fireEvent.scroll(viewport);
    });
    expect(viewport.querySelectorAll(".timeline-item").length).toBeGreaterThan(0);
    act(() => {
      rerender(<TimelineView items={items} threadKey="new-thread" />);
    });
    expect(viewport.scrollTop).toBe(120_000);
    vi.restoreAllMocks();
  });

  it("keeps the exact reading row while crossing 100 to 101 variable-height rows", () => {
    const items = Array.from({ length: 101 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    const { rerender } = render(<TimelineView items={items.slice(0, 100)} threadKey="threshold" />);
    const viewport = screen.getByTestId("scroll-viewport");
    const content = viewport.querySelector<HTMLElement>('[role="log"]')!;
    const box = (top: number, height = 0) => ({
      top, bottom: top + height, left: 0, right: 0, width: 0, height,
      x: 0, y: top, toJSON: () => ({}),
    });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 18_000, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return box(0, 600);
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      if (!group) return box(0);
      let top = 52;
      for (const sibling of content.children) {
        if (sibling === group) break;
        if (sibling.hasAttribute("data-virtual-group")) top += 172;
        if (sibling.hasAttribute("data-timeline-spacer")) top += Number.parseFloat((sibling as HTMLElement).style.height) + 12;
      }
      return box(top - viewport.scrollTop, 160);
    });
    act(() => {
      viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight;
      fireEvent.scroll(viewport);
    });
    act(() => {
      viewport.scrollTop = 52 + 50 * 172 - 100;
      fireEvent.scroll(viewport);
    });
    const before = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="51.0"]')!;
    const top = before.getBoundingClientRect().top;
    rerender(<TimelineView items={items} threadKey="threshold" />);
    const after = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="51.0"]');
    expect(after).not.toBeNull();
    expect(Math.abs(after!.getBoundingClientRect().top - top)).toBeLessThan(3);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(80);
    vi.restoreAllMocks();
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

  it("keeps the reader when a buffered row grows after layout", () => {
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
    const items = Array.from({ length: 300 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Reply ${index + 1}`,
    }));
    render(<TimelineView items={items} threadKey="large-row" />);
    const viewport = screen.getByTestId("scroll-viewport");
    const content = viewport.querySelector<HTMLElement>('[role="log"]')!;
    let tallHeight = 80;
    const box = (top: number, height = 0) => ({
      top, bottom: top + height, left: 0, right: 0, width: 0, height,
      x: 0, y: top, toJSON: () => ({}),
    });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 120_000, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return box(0, 600);
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      if (!group) return box(0);
      const heightOf = (node: Element) => node.querySelector('[data-item-id="50.0"]') ? tallHeight : 80;
      let top = 52;
      for (const sibling of content.children) {
        if (sibling === group) break;
        if (sibling.hasAttribute("data-virtual-group")) top += heightOf(sibling) + 12;
        if (sibling.hasAttribute("data-timeline-spacer")) top += Number.parseFloat((sibling as HTMLElement).style.height) + 12;
      }
      return box(top - viewport.scrollTop, heightOf(group));
    });
    act(() => {
      viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight;
      fireEvent.scroll(viewport);
    });
    act(() => {
      viewport.scrollTop = 52 + 53 * 92 - 100;
      fireEvent.scroll(viewport);
    });
    const anchor = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="54.0"]');
    const tall = viewport.querySelector<HTMLElement>('[data-virtual-group]:has([data-item-id="50.0"])');
    expect(anchor).not.toBeNull();
    expect(tall).not.toBeNull();
    const top = anchor!.getBoundingClientRect().top;
    tallHeight = 90_000;
    const observer = observers.findLast(({ targets }) => targets.has(tall!));
    expect(observer).toBeDefined();
    act(() => observer!.callback([{
      target: tall!, borderBoxSize: [{ blockSize: tallHeight }],
    } as unknown as ResizeObserverEntry], {} as ResizeObserver));
    const after = viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="54.0"]');
    expect(after).not.toBeNull();
    expect(Math.abs(after!.getBoundingClientRect().top - top)).toBeLessThan(3);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(80);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("mounts only the viewport and nearby rows while retaining the full scroll space", async () => {
    const items = Array.from({ length: 1200 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`,
      kind: "agent_chat",
      payload: `Reply ${index + 1}`,
    }));
    render(<TimelineView items={items} threadKey="deep-history" />);
    const viewport = screen.getByTestId("scroll-viewport");
    Object.defineProperty(viewport, "clientHeight", { value: 720, configurable: true });
    act(() => {
      viewport.scrollTop = 48_000;
      fireEvent.scroll(viewport);
    });
    await waitFor(() => {
      expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
      expect(viewport.querySelector('[data-timeline-spacer="before"]')).not.toBeNull();
      expect(viewport.querySelector('[data-timeline-spacer="after"]')).not.toBeNull();
    });
  });

  it("windows the children of one long expanded work block", async () => {
    const items = Array.from({ length: 600 }, (_, index) => makeItem({
      item_id: `${index + 1}.0`,
      kind: "agent_reasoning",
      payload: `Thought ${index + 1}`,
    }));
    render(<TimelineView items={items} threadKey="deep-run" />);
    const viewport = screen.getByTestId("scroll-viewport");
    expect(viewport.querySelectorAll('[data-turn-expanded="true"]').length).toBe(1);
    expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
    expect(viewport.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
    Object.defineProperty(viewport, "clientHeight", { value: 720, configurable: true });
    act(() => {
      viewport.scrollTop = 20_000;
      fireEvent.scroll(viewport);
    });
    await waitFor(() => {
      expect(viewport.querySelectorAll(".timeline-item").length).toBeLessThan(60);
      expect(viewport.querySelector('[data-timeline-spacer="turn-after"]')).not.toBeNull();
    });
  });
});


it("preserves every paragraph of a human message during impersonation", () => {
  const item = makeItem({ kind: "inbound_chat", source: "user", payload: "First paragraph.\n\nSecond paragraph.",
    impersonation: { agent_id: 42, session_id: 0, name: "Fix login", executor_name: "Claude: helper", provider: "claude" } });
  const { container } = render(<ItemView item={item} streaming={false} />);
  expect(container.querySelector("[data-testid=chat-markdown]")?.textContent).toContain("First paragraph.");
  expect(container.textContent).toContain("Second paragraph.");
});


it("renders image-only human messages during impersonation as thumbnails", () => {
  const item = makeItem({ kind: "inbound_chat", source: "user", payload: "[image]",
    images: ["/api/agents/42/uploads/screenshot.png"],
    impersonation: { agent_id: 42, session_id: 0, name: "Fix login", executor_name: "Claude: helper", provider: "claude" } });
  const { container } = render(<ItemView item={item} streaming={false} />);
  expect(container.querySelector("img")?.getAttribute("src")).toContain("/api/agents/42/uploads/screenshot.png");
  expect(container.textContent).not.toContain("[image]");
});


it("restores expanded work and detail blocks when returning to an agent", () => {
  setToggleState({ detailsMode: "none" });
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
