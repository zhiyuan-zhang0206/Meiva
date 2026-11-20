import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, timelineLayout } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { TimelineView } from "..";
import { READING_TOP_PX } from "../anchor/dom";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  cleanupTimelineTest();
});

// meiva:note
// The send anchor end to end: the page hands the timeline the inbound its receipt named, the row
// mounts, and a following reader's own message is parked at the reading offset over a bottom
// reserve. Growth then moves the reply, never the reader, until the reader asks for the tail again.
// A reader in history keeps their place; the arrow parks the message for them.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md

const earlier = makeItem({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:earlier", inbound_id: 3, source: "user" });
const sent = makeItem({ item_id: "2.0", kind: "inbound_chat", payload: "envelope:mine", inbound_id: 7, source: "user" });
const reply = makeItem({ item_id: "3.0", kind: "agent_chat", payload: "reply" });
const peer = makeItem({ item_id: "3.0", kind: "inbound_chat", payload: "envelope:peer", source: "agent:9" });

// meiva:note
// The earlier message overflows the 600px viewport; the reply's height is what streams.
function layout() {
  const heights: Record<string, number> = { "1.0": 1000, "2.0": 100, "3.0": 100, "4.0": 100 };
  const model = timelineLayout({ rowHeight: (row) => heights[row.dataset.itemId ?? ""] });
  // meiva:note
  // The browser echoes each programmatic write with a scroll event.
  const settle = () => model.echo();
  const grow = (id: string, height: number) => {
    heights[id] = height;
    model.resize();
  };
  return { ...model, heights, settle, grow };
}
const send = (inboundId: number) => act(() => { useTimelineStore.getState().requestSendAnchor(inboundId); });
const arrow = () => screen.queryByTestId("scroll-to-bottom");

describe("send anchor", () => {
  it("parks the sent message in a short conversation opened on its first unseen delivery", () => {
    const heights: Record<string, number> = { "1.0": 100, "2.0": 100 };
    const model = timelineLayout({ rowHeight: (row) => heights[row.dataset.itemId ?? ""] });
    const view = render(<TimelineView items={[earlier]} reveal={{ decided: true, itemId: "1.0" }} />);
    // meiva:note
    // A conversation shorter than the viewport has no scroll range: opened on a row, the reader is
    // still at its end, so a send parks like a follower's.
    expect(model.top()).toBe(0);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} reveal={{ decided: true, itemId: "1.0" }} />);
    model.echo();
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBeGreaterThan(0);
  });

  it("parks the sent message at the reading offset and holds it while the reply streams", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    expect(model.top()).toBe(model.naturalEnd());

    send(7);
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.reserve()).toBe(0);

    view.rerender(<TimelineView items={[earlier, sent]} />);
    model.settle();
    const parked = model.top();
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    // meiva:note
    // The reserve is exactly the room the park needs: the parked place is the end of the range.
    expect(model.reserve()).toBeGreaterThan(0);
    expect(parked).toBe(model.max());
    expect(arrow()).toBeNull();

    // meiva:note
    // The reply streams into the reserve: every pixel of growth is a pixel less reserve.
    let reserve = model.reserve();
    let natural = model.naturalEnd();
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    expect(model.top()).toBe(parked);
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBe(reserve - (model.naturalEnd() - natural));

    reserve = model.reserve();
    natural = model.naturalEnd();
    model.grow("3.0", 300);
    expect(model.top()).toBe(parked);
    expect(model.reserve()).toBe(reserve - (model.naturalEnd() - natural));
    expect(model.reserve()).toBeGreaterThan(0);
    expect(arrow()).toBeNull();

    // meiva:note
    // Once the reply fills the viewport the reserve is gone and the arrow offers the rest.
    model.grow("3.0", 1200);
    expect(model.top()).toBe(parked);
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBe(0);
    expect(arrow()).toBeTruthy();

    model.grow("3.0", 2400);
    expect(model.top()).toBe(parked);
    expect(model.reserve()).toBe(0);
  });

  it("parks only the row the receipt named", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    send(7);
    // meiva:note
    // Another human message (another tab's send) lands first: it is not the one this receipt named.
    const other = { ...sent, item_id: "3.0", inbound_id: 9 };
    view.rerender(<TimelineView items={[earlier, other]} />);
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.reserve()).toBe(0);
    view.rerender(<TimelineView items={[earlier, other, { ...sent, item_id: "4.0" }]} />);
    expect(model.rowTop("4.0")).toBe(READING_TOP_PX);
  });

  // meiva:note
  // A browser clamps scrollTop into a shorter scroll range during layout, before ResizeObserver or any
  // scroll event. The writer does not count that clamp as reader drift, so the sent row keeps its
  // offset while the reserve grows to hold it.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  it("keeps the sent row aligned when the composer clears and the viewport grows", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} />);
    model.settle();
    const reserve = model.reserve();

    model.setViewportHeight(634);
    // meiva:note
    // The parked place is the end of the range, so the browser clamps scrollTop into the shorter
    // range during layout, before ResizeObserver runs and before any scroll event: not the reader.
    model.drift(model.top());
    model.resize();
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBe(reserve + 34);
    expect(model.top()).toBe(model.max());
  });

  it("keeps a reader in history in place on send; the arrow then parks the message, and next goes to the tail", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    model.scrollTo(100);
    send(7);
    // meiva:note
    // A peer agent's message lands after the send: the latest human message is still the sent one.
    const answer = { ...reply, item_id: "4.0" };
    view.rerender(<TimelineView items={[earlier, sent, peer, answer]} />);
    expect(model.top()).toBe(100);

    act(() => { arrow()!.click(); });
    model.settle();
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBeGreaterThan(0);
    model.grow("4.0", 300);
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);

    model.grow("4.0", 1600);
    expect(model.reserve()).toBe(0);
    act(() => { arrow()!.click(); });
    expect(model.smoothTarget()).toBe(model.naturalEnd());
    model.finishSmooth();
    expect(model.top()).toBe(model.naturalEnd());
    expect(arrow()).toBeNull();
  });

  it("keeps the reserve while the reader moves inside it, and releases it at the natural end", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} threadKey="42" scrollMemoryKey="send-release" />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} threadKey="42" scrollMemoryKey="send-release" />);
    model.settle();
    const parked = model.top();
    const natural = model.naturalEnd();
    expect(parked).toBeGreaterThan(natural + 100);

    model.scrollTo(parked - 50);
    expect(model.top()).toBe(parked - 50);
    expect(model.reserve()).toBeGreaterThan(0);
    model.scrollTo(natural + 1);
    expect(model.top()).toBe(natural + 1);
    expect(model.reserve()).toBeGreaterThan(0);

    model.scrollTo(natural);
    expect(model.top()).toBe(natural);
    expect(model.reserve()).toBe(0);
    model.resize();
    expect(model.reserve()).toBe(0);

    // meiva:note
    // A return to the agent keeps the spent reserve spent.
    model.scrollTo(natural - 200);
    view.rerender(<TimelineView key="43" items={[]} threadKey="43" />);
    view.rerender(<TimelineView key="42" items={[earlier, sent]} threadKey="42" scrollMemoryKey="send-release" />);
    expect(model.top()).toBe(natural - 200);
    expect(model.reserve()).toBe(0);
  });

  it("releases the reserve when touch inertia carries the reader past the natural end after the finger lifts", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} />);
    model.settle();
    const natural = model.naturalEnd();

    model.touchStart();
    model.scrollTo(model.top() - 40);
    model.touchEnd();
    expect(model.reserve()).toBeGreaterThan(0);
    model.scrollTo(natural + 20);
    expect(model.reserve()).toBeGreaterThan(0);
    model.scrollTo(natural - 30);
    expect(model.reserve()).toBe(0);
    expect(model.top()).toBe(natural - 30);
  });

  it("a new send from history renews the released space without moving the reader", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} />);
    model.settle();
    model.scrollTo(model.naturalEnd() - 300);
    expect(model.reserve()).toBe(0);
    const reading = model.top();

    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} />);
    expect(model.top()).toBe(reading);
    act(() => { arrow()!.click(); });
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    expect(model.reserve()).toBeGreaterThan(0);
  });

  it("an upward wheel leaves a followed tail before the next output arrives", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} />);
    const tail = model.top();
    expect(tail).toBe(model.naturalEnd());
    act(() => { model.viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: -8, bubbles: true })); });
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    expect(model.top()).toBe(tail);
    model.scrollTo(tail - 8);
    model.grow("3.0", 400);
    expect(model.top()).toBe(tail - 8);
  });

  it("follows again once the reader scrolls back to the bottom", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    model.settle();
    model.grow("3.0", 1200);
    const parked = model.top();
    expect(model.top()).toBeLessThan(model.naturalEnd());

    model.scrollTo(model.naturalEnd() - 10);
    model.grow("3.0", 1800);
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.top()).toBeGreaterThan(parked);
  });

  it("keeps the centered arrow when a work block starts and leaves the sent message in place", () => {
    const heights: Record<string, number> = { "1.0": 1000, "2.0": 100 };
    let block = 40;
    const model = timelineLayout({
      rowHeight: (row) => heights[row.dataset.itemId ?? ""],
      height: (element) => element.matches("[data-turn-id]") ? block : undefined,
    });
    const view = render(<TimelineView items={[earlier]} turnActive />);
    send(7);
    view.rerender(<TimelineView items={[earlier, sent]} turnActive />);
    model.echo();
    const work = makeItem({ item_id: "3.0", kind: "agent_code", payload: "print(1)" });
    view.rerender(<TimelineView items={[earlier, sent, work]} turnActive />);
    block = 2000;
    model.resize();
    expect(model.rowTop("2.0")).toBe(READING_TOP_PX);
    const button = screen.getByRole("button", { name: "Scroll down" });
    expect(button.className).toContain("left-1/2");
    expect(screen.getByTestId("scroll-to-bottom-arrow")).toBeTruthy();
    expect(screen.queryByTestId("scroll-to-bottom-working")).toBeNull();
  });
});
