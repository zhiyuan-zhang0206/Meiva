import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { TimelineView } from "..";
import { parkReserve, parkScrollTop } from "../model/use-viewport-arrivals";
afterEach(cleanupTimelineTest);

describe("send park decisions", () => {
  it("places the sent row at the reading offset and never above the document start", () => {
    expect(parkScrollTop(1300, 56)).toBe(1244);
    expect(parkScrollTop(20, 56)).toBe(0);
  });

  it("reserves the height the reply has not written yet and gives it back as it grows", () => {
    expect(parkReserve(1244, 600, 1400)).toBe(444);
    expect(parkReserve(1244, 600, 1700)).toBe(144);
    expect(parkReserve(1244, 600, 2600)).toBe(0);
  });
});

// meiva:note
// The send anchor end to end: the page hands the timeline the inbound its
// receipt named, the row mounts, and the reader's own message is parked a fifth
// of the viewport down over a bottom reserve. Everything after that is the
// point of the change - growth moves the reply, never the reader - until the
// reader asks for the tail again.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

let top = 0;
let content = 0;
let humanTop = 0;
let viewportHeight = 0;
let resizeCallbacks: Set<() => void>;

const viewport = () => screen.getByTestId("scroll-viewport");
const reserve = () => document.querySelector<HTMLElement>("[data-send-reserve]");
const reservePx = () => Number.parseFloat(reserve()?.style.height ?? "0") || 0;
function resize() { act(() => { resizeCallbacks.forEach((deliver) => deliver()); }); }

beforeEach(() => {
  top = 0;
  content = 1000;
  humanTop = 1300;
  viewportHeight = 600;
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
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => viewportHeight);
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
    const y = this.hasAttribute("data-send-reserve") ? content - top
      : this.dataset.itemId === "2.0" ? humanTop - top
      : this.dataset.itemId === "1.0" ? 56 - top : 0;
    return { top: y, bottom: y + 120, height: 120, left: 0, right: 400, width: 400 } as DOMRect;
  });
});

const earlier = makeItem({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:earlier", inbound_id: 3, source: "user" });
const sent = makeItem({ item_id: "2.0", kind: "inbound_chat", payload: "envelope:mine", inbound_id: 7, source: "user" });

describe("send anchor", () => {
  it("parks the sent message below the header and holds it while the reply streams", () => {
    const view = render(<TimelineView items={[earlier]} />);
    content = 1000;
    expect(top).toBe(400);

    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    expect(top).toBe(400);
    expect(reservePx()).toBe(0);

    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    // meiva:note
    // The row sits at document 1300 and the reading position is the 44px bar
    // plus a 12px gap, so the anchored position is 1244 - reachable only
    // because the reserve adds the 444px the reply has not written yet.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
    expect(top).toBe(1244);
    expect(reservePx()).toBe(444);
    // meiva:note
    // The viewport is at the bottom of a padded document here, so the control is
    // the reader's only way back into following and stays shown.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();

    content = 1700;
    resize();
    expect(top).toBe(1244);
    expect(reservePx()).toBe(144);

    content = 1900;
    resize();
    expect(top).toBe(1244);
    expect(reservePx()).toBe(0);
    expect(screen.getByTestId("scroll-to-bottom")).toBeTruthy();

    content = 2600;
    resize();
    expect(top).toBe(1244);
  });

  it("keeps the sent row aligned when the composer clears and grows the viewport", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(top).toBe(1244);

    viewportHeight = 634;
    viewport().scrollTop = top;
    expect(top).toBe(1210);
    resize();
    expect(top).toBe(1244);
    expect(reservePx()).toBe(478);
  });

  it("keeps a clicked question aligned through a layout scroll before ResizeObserver", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { top = 100; viewport().dispatchEvent(new Event("scroll")); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    act(() => { screen.getByTestId("scroll-to-bottom").click(); });
    expect(top).toBe(1244);

    humanTop += 350;
    content = 2800;
    act(() => { viewport().dispatchEvent(new Event("scroll")); });
    expect(top).toBe(1594);

    const scrollTo = vi.spyOn(viewport(), "scrollTo");
    act(() => { screen.getByTestId("scroll-to-bottom").click(); });
    expect(scrollTo).toHaveBeenCalledWith({ top: 2200, behavior: "smooth" });
  });

  it("keeps history in place on send and uses the current position for each arrow click", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { top = 100; viewport().dispatchEvent(new Event("scroll")); });
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 2600;
    const peer = makeItem({ item_id: "3.0", kind: "inbound_chat", payload: "envelope:peer", source: "agent:9" });
    view.rerender(<TimelineView items={[earlier, sent, peer]} />);
    expect(top).toBe(100);

    const arrow = screen.getByRole("button", { name: "Scroll down" });
    act(() => { arrow.click(); });
    expect(top).toBe(1244);
    content = 2800;
    resize();
    expect(top).toBe(1244);

    act(() => { top = 1500; viewport().dispatchEvent(new Event("scroll")); });
    const scrollTo = vi.spyOn(viewport(), "scrollTo");
    act(() => { arrow.click(); });
    expect(scrollTo).toHaveBeenCalledWith({ top: 2200, behavior: "smooth" });
  });

  it("makes room to align the latest human message manually, even after a send from history", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { top = 100; viewport().dispatchEvent(new Event("scroll")); });
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(top).toBe(100);
    expect(reservePx()).toBe(444);

    act(() => { viewport().scrollTop = viewport().scrollHeight; viewport().dispatchEvent(new Event("scroll")); });
    expect(top).toBe(1244);
    content = 1500;
    resize();
    expect(top).toBe(1244);
    expect(reservePx()).toBe(344);
  });

  it("allows an upward wheel to leave a followed tail before the next output arrives", () => {
    render(<TimelineView items={[earlier]} />);
    expect(top).toBe(400);
    act(() => { viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: -8, bubbles: true })); });
    act(() => { top = 392; viewport().dispatchEvent(new Event("scroll")); });
    content = 1200;
    resize();
    expect(top).toBe(392);
  });

  it("follows again once the reader scrolls to the bottom", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    content = 1900;
    resize();
    expect(top).toBe(1244);

    act(() => {
      viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: 80, bubbles: true }));
      top = 1300;
      viewport().dispatchEvent(new Event("scroll"));
    });
    content = 2400;
    resize();
    expect(top).toBe(1800);
  });

  it("follows again when the jump-to-latest control is clicked", () => {
    const view = render(<TimelineView items={[earlier]} />);
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(top).toBe(1244);
    vi.spyOn(viewport(), "scrollTo").mockImplementation((options?: ScrollToOptions | number) => {
      if (typeof options === "object") viewport().scrollTop = options.top ?? 0;
    });
    act(() => {
      screen.getByTestId("scroll-to-bottom").click();
      viewport().dispatchEvent(new Event("scroll"));
      viewport().dispatchEvent(new Event("scrollend"));
    });
    expect(top).toBe(800);
    expect(reservePx()).toBe(444);
    content = 2400;
    resize();
    expect(top).toBe(1800);
  });

  it("keeps the centered arrow when a work block starts and leaves the sent message in place", () => {
    const view = render(<TimelineView items={[earlier]} turnActive />);
    act(() => { useTimelineStore.getState().requestSendAnchor(7); });
    content = 1400;
    view.rerender(<TimelineView items={[earlier, sent]} turnActive />);
    const work = makeItem({ item_id: "3.0", kind: "agent_code", payload: "print(1)" });
    content = 2600;
    view.rerender(<TimelineView items={[earlier, sent, work]} turnActive />);
    resize();
    expect(top).toBe(1244);
    expect(screen.getByRole("button", { name: "Scroll down" }).className).toContain("left-1/2");
    expect(screen.getByTestId("scroll-to-bottom-arrow")).toBeTruthy();
    expect(screen.queryByTestId("scroll-to-bottom-working")).toBeNull();
  });
});
