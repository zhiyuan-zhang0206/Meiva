import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);

// meiva:note
// The send anchor end to end: the page hands the timeline the inbound its
// receipt named, the row mounts, and the reader's own message is parked a fifth
// of the viewport down over a bottom reserve. Everything after that is the
// point of the change - growth moves the reply, never the reader - until the
// reader asks for the tail again.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

let top = 0;
let content = 0;
let resizeCallbacks: Set<() => void>;

const viewport = () => screen.getByTestId("scroll-viewport");
const reserve = () => document.querySelector<HTMLElement>("[data-send-reserve]");
const reservePx = () => Number.parseFloat(reserve()?.style.height ?? "0") || 0;
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }

beforeEach(() => {
  top = 0;
  content = 1000;
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
    return this.getAttribute("data-slot") === "scroll-area-viewport" ? content + reservePx() : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.hasAttribute("data-send-reserve") ? reservePx() : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => top);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "set").mockImplementation(function (this: HTMLElement, value: number) {
    top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const y = this.dataset.itemId === "2.0" ? 900 : 0;
    return { top: y, bottom: y + 120, height: 120, left: 0, right: 400, width: 400 } as DOMRect;
  });
});

const earlier = makeItem({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:earlier", inbound_id: 3, source: "user" });
const sent = makeItem({ item_id: "2.0", kind: "inbound_chat", payload: "envelope:mine", inbound_id: 7, source: "user" });

describe("send anchor", () => {
  it("pins the sent message to the bottom and keeps following while the reply streams", () => {
    const view = render(<TimelineView items={[earlier]} />);
    content = 1000;
    expect(top).toBe(400);

    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    expect(top).toBe(400);
    expect(reservePx()).toBe(0);

    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(top).toBe(800);
    expect(reservePx()).toBe(0);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    content = 1700;
    resize();
    expect(top).toBe(1100);
    expect(reservePx()).toBe(0);

    content = 1900;
    resize();
    expect(top).toBe(1300);
    expect(reservePx()).toBe(0);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });

  it("re-sticks to the bottom on send even if the reader had scrolled up earlier", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { top = 100; viewport().dispatchEvent(new Event("scroll")); });
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();

    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(top).toBe(800);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    content = 2400;
    resize();
    expect(top).toBe(1800);
  });
});
