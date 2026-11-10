import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import {
  ChatMarkdownMock,
  ScrollAreaMock,
  cleanupTimelineTest,
  displayLimits,
  makeItem,
  render,
  timelineQueryClient,
} from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  cleanupTimelineTest();
});

const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];

function geometry(viewport: HTMLElement) {
  Object.defineProperty(viewport, "scrollHeight", { value: 5000, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
}

// meiva:note
// Nothing to scroll to means nothing to offer: at the bottom the control is not
// in the DOM, so a hover cannot turn it into an arrow over a click that does
// nothing (user report 2026-10-30). A refresh in flight does not override it.
// The state is the measured distance to the bottom, so appended content
// re-decides it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
describe("scroll-to-bottom control presence", () => {
  it("is absent at the bottom, including while a refresh is in flight", () => {
    const { rerender } = render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport);
    viewport.scrollTop = 4400;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    rerender(<TimelineView items={items} loadingNew />);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
    expect(screen.queryByTestId("latest-loading-ring")).toBeNull();
  });

  it("appears away from the bottom and is gone again once the click returns the reader", () => {
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport);
    const scrollTo = vi.spyOn(viewport, "scrollTo").mockImplementation(() => undefined);
    viewport.scrollTop = 700;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    const button = screen.getByTestId("scroll-to-bottom");
    expect(button.getAttribute("aria-label")).toBe("Scroll to bottom");
    expect(button.className).not.toContain("opacity-0");
    expect(button.className).not.toContain("pointer-events-none");

    fireEvent.click(button);
    expect(scrollTo).toHaveBeenCalledWith({ top: 5000, behavior: "smooth" });
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("reaches the true bottom in one click across multi-wave virtualized height shifts", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 5);
    const observers: { cb: ResizeObserverCallback; targets: Set<Element> }[] = [];
    vi.stubGlobal("ResizeObserver", class {
      readonly record: (typeof observers)[number];
      constructor(cb: ResizeObserverCallback) {
        this.record = { cb, targets: new Set() };
        observers.push(this.record);
      }
      observe(target: Element) { this.record.targets.add(target); }
      unobserve(target: Element) { this.record.targets.delete(target); }
      disconnect() { this.record.targets.clear(); }
    });

    const rows = Array.from({ length: 80 }, (_, i) =>
      makeItem({ item_id: `${i + 1}.0`, kind: "agent_chat", payload: `Reply ${i + 1}` }),
    );
    render(<TimelineView items={rows} threadKey="ride-virtual-waves" />);
    const viewport = screen.getByTestId("scroll-viewport");
    const content = viewport.querySelector<HTMLElement>('[role="log"]')!;

    const rowHeight = (group: Element) => {
      const id = Number.parseInt(group.querySelector<HTMLElement>(".timeline-item")?.dataset.itemId ?? "1");
      return id % 3 === 0 ? 340 : 36;
    };
    const computeContentHeight = () => {
      let total = 52;
      for (const child of content.children) {
        if (child.hasAttribute("data-virtual-group")) total += rowHeight(child) + 12;
        else if (child.hasAttribute("data-timeline-spacer")) {
          total += Number.parseFloat((child as HTMLElement).style.height) + 12;
        }
      }
      return total;
    };

    let top = 400;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { configurable: true, get: () => computeContentHeight() },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (value: number) => {
          top = Math.max(0, Math.min(value, Math.max(0, computeContentHeight() - 600)));
        },
      },
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this === viewport) return { top: 0, bottom: 600, height: 600 } as DOMRect;
      const group = this.matches("[data-virtual-group]") ? this : this.closest<HTMLElement>("[data-virtual-group]");
      if (!group) return { top: 0, bottom: 0, height: 0 } as DOMRect;
      let y = 52;
      for (const sibling of content.children) {
        if (sibling === group) break;
        if (sibling.hasAttribute("data-virtual-group")) y += rowHeight(sibling) + 12;
        else if (sibling.hasAttribute("data-timeline-spacer")) {
          y += Number.parseFloat((sibling as HTMLElement).style.height) + 12;
        }
      }
      const h = rowHeight(group);
      return { top: y - top, bottom: y - top + h, height: h } as DOMRect;
    });

    act(() => {
      top = 400;
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(viewport.querySelector('[data-timeline-spacer="after"]')).not.toBeNull();
    const button = screen.getByTestId("scroll-to-bottom");

    let smoothTarget = 0;
    vi.spyOn(viewport, "scrollTo").mockImplementation((opts?: ScrollToOptions | number) => {
      if (typeof opts === "object" && opts.top !== undefined) smoothTarget = opts.top;
    });

    fireEvent.click(button);
    expect(smoothTarget).toBeGreaterThan(2000);

    act(() => {
      top = Math.min(smoothTarget, viewport.scrollHeight - 600);
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new Event("scrollend"));
    });
    for (let wave = 0; wave < 4; wave += 1) {
      act(() => {
        viewport.dispatchEvent(new Event("scroll"));
        for (const { cb, targets } of observers) {
          const entries = [...targets].map((target) => ({
            target,
            borderBoxSize: [{ blockSize: target instanceof HTMLElement ? target.getBoundingClientRect().height : 0 }],
          })) as unknown as ResizeObserverEntry[];
          if (entries.length > 0) cb(entries, {} as ResizeObserver);
        }
      });
    }

    expect(viewport.querySelector('[data-timeline-spacer="after"]')).toBeNull();
    expect(viewport.querySelector('.timeline-item[data-item-id="80.0"]')).not.toBeNull();
    expect(viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop).toBeLessThanOrEqual(1);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("drives to the true bottom in one click even when an expanded live work block grows", () => {
    timelineQueryClient.setQueryData(SETTINGS_QUERY_KEY, {
      ...USER_SETTING_DEFAULTS,
      "display.expand_working_block": true,
    });
    const observers: ResizeObserverCallback[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { observers.push(cb); }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    });

    const liveItems = [
      makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Run diagnostics" }),
      ...Array.from({ length: 15 }, (_, i) =>
        makeItem({ item_id: `2.${i}`, kind: "agent_reasoning", payload: `Step ${i + 1}` }),
      ),
    ];
    render(<TimelineView items={liveItems} threadKey="live-block-ride" turnActive />);
    const viewport = screen.getByTestId("scroll-viewport");
    let height = 5000;
    let top = 4400;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (v: number) => { top = Math.max(0, Math.min(v, height - 600)); },
      },
    });
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
      top = 800;
      viewport.dispatchEvent(new WheelEvent("wheel", { deltaY: -120, bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });

    height = 7000;
    act(() => { for (const cb of observers) cb([], {} as ResizeObserver); });
    expect(top).toBe(800);

    vi.spyOn(viewport, "scrollTo").mockImplementation(() => {
      top = 6400;
      height = 14000;
    });

    fireEvent.click(screen.getByTestId("scroll-to-bottom"));
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new Event("scrollend"));
    });
    height = 16500;
    act(() => { for (const cb of observers) cb([], {} as ResizeObserver); });

    expect(top).toBe(15900);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("defers follower oldest-item trimming until the scroll-to-bottom ride arrives", () => {
    const rafQueue: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      rafQueue.push(cb);
      return rafQueue.length;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    const fullItems = Array.from({ length: 12 }, (_, i) =>
      makeItem({ item_id: `${i + 1}.0`, kind: "agent_chat", payload: `Msg ${i + 1}` }),
    );
    const tailItems = fullItems.slice(6);
    useTimelineStore.getState().switchThread(99, tailItems, true);
    const { rerender } = render(
      <TimelineView items={tailItems} threadKey="trim-after-ride" retainedItemsMax={6} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    let height = 4000;
    let top = 3400;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (v: number) => { top = Math.max(0, Math.min(v, height - 600)); },
      },
    });
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
      top = 400;
      viewport.dispatchEvent(new WheelEvent("wheel", { deltaY: -120, bubbles: true }));
      viewport.dispatchEvent(new Event("scroll"));
    });
    act(() => {
      useTimelineStore.getState().beginLoadOlder();
      useTimelineStore.getState().prependOlder(fullItems.slice(0, 6), false);
    });
    rerender(
      <TimelineView items={useTimelineStore.getState().items} threadKey="trim-after-ride" retainedItemsMax={6} />,
    );
    expect(useTimelineStore.getState().items.length).toBe(12);
    vi.spyOn(viewport, "scrollTo").mockImplementation(() => undefined);

    fireEvent.click(screen.getByTestId("scroll-to-bottom"));

    act(() => {
      top = 1500;
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(useTimelineStore.getState().items.length).toBe(12);

    act(() => {
      top = 3400;
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new Event("scrollend"));
    });
    for (let i = 0; i < 4 && rafQueue.length > 0; i += 1) {
      const next = rafQueue.shift()!;
      act(() => { next(performance.now()); });
      const currentStoreItems = useTimelineStore.getState().items;
      if (currentStoreItems.length < 12) {
        height = 2200;
        top = 1600;
        rerender(<TimelineView items={currentStoreItems} threadKey="trim-after-ride" retainedItemsMax={6} />);
      }
    }
    expect(useTimelineStore.getState().items.length).toBeLessThanOrEqual(7);
    expect(viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop).toBeLessThanOrEqual(1);
  });

  it("aborts the scroll-to-bottom ride immediately when the user wheels upward", () => {
    const observers: ResizeObserverCallback[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { observers.push(cb); }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    });

    render(<TimelineView items={items} threadKey="wheel-abort-ride" />);
    const viewport = screen.getByTestId("scroll-viewport");
    let top = 700;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { value: 5000, configurable: true },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (v: number) => { top = v; },
      },
    });
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    vi.spyOn(viewport, "scrollTo").mockImplementation(() => undefined);

    fireEvent.click(screen.getByTestId("scroll-to-bottom"));
    act(() => {
      top = 1800;
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new WheelEvent("wheel", { deltaY: -80, bubbles: true, cancelable: true }));
      top = 1720;
      viewport.dispatchEvent(new Event("scroll"));
      viewport.dispatchEvent(new Event("scrollend"));
      for (const cb of observers) cb([], {} as ResizeObserver);
    });

    expect(top).toBe(1720);
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();
  });

  it("jumps directly to the bottom when expanding a working TurnBlock and follows streaming growth, while keeping a settled TurnBlock at its top", () => {
    const observers: ResizeObserverCallback[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { observers.push(cb); }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    });

    const workItems = [
      makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Check progress" }),
      ...Array.from({ length: 20 }, (_, i) =>
        makeItem({ item_id: `2.${i}`, kind: "agent_reasoning", payload: `Step ${i + 1}` }),
      ),
    ];
    const { rerender } = render(<TimelineView items={workItems} threadKey="expand-live-vs-settled" turnActive />);
    const viewport = screen.getByTestId("scroll-viewport");
    let height = 1000;
    let top = 400;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600, configurable: true },
      scrollHeight: { configurable: true, get: () => height },
      scrollTop: {
        configurable: true,
        get: () => top,
        set: (v: number) => { top = Math.max(0, Math.min(v, height - 600)); },
      },
    });

    height = 3600;
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    expect(top).toBe(3000);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    height = 4400;
    act(() => { for (const cb of observers) cb([], {} as ResizeObserver); });
    expect(top).toBe(3800);

    fireEvent.click(screen.getByTestId("turn-toggle"));
    height = 1000;
    top = 400;
    rerender(<TimelineView items={workItems} threadKey="expand-live-vs-settled" turnActive={false} />);
    height = 3600;
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    expect(top).toBe(400);
  });
});
