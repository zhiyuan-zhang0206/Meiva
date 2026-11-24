import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBrowser } from "@/lib/browser/testing";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, timelineLayout } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  cleanupTimelineTest();
});

// meiva:note
// A send is not a placement: the reader's own message arrives like any other row. A reader who
// follows the tail keeps following it, a reader in history keeps their place, and no space opens
// below the conversation (user ruling 2026-11-20).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md

const earlier = makeItem({ item_id: "1.0", kind: "inbound_chat", payload: "envelope:earlier", inbound_id: 3, source: "user" });
const sent = makeItem({ item_id: "2.0", kind: "inbound_chat", payload: "envelope:mine", inbound_id: 7, source: "user" });
const reply = makeItem({ item_id: "3.0", kind: "agent_chat", payload: "reply" });

// meiva:note
// The earlier message overflows the 600px viewport; the reply's height is what streams.
function layout() {
  const heights: Record<string, number> = { "1.0": 1000, "2.0": 100, "3.0": 100 };
  const model = timelineLayout({ rowHeight: (row) => heights[row.dataset.itemId ?? ""] });
  const grow = (id: string, height: number) => {
    heights[id] = height;
    model.resize();
  };
  return { ...model, grow };
}
const arrow = () => screen.queryByTestId("scroll-to-bottom");

describe("a send", () => {
  it("keeps a following reader on the tail while the sent message and the reply arrive", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    expect(model.top()).toBe(model.naturalEnd());
    view.rerender(<TimelineView items={[earlier, sent]} />);
    model.echo();
    expect(model.top()).toBe(model.naturalEnd());
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    model.grow("3.0", 1200);
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.max()).toBe(model.naturalEnd());
    expect(arrow()).toBeNull();
  });

  it("leaves a reader in history where they are, and the arrow takes them to the tail", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    model.scrollTo(100);
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    model.grow("3.0", 800);
    expect(model.top()).toBe(100);
    expect(model.max()).toBe(model.naturalEnd());
    act(() => { screen.getByRole("button", { name: "Scroll down" }).click(); });
    expect(model.smoothTarget()).toBe(model.naturalEnd());
    model.finishSmooth();
    expect(model.top()).toBe(model.naturalEnd());
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
    const view = render(<TimelineView items={[earlier, sent, reply]} />);
    model.grow("3.0", 1200);
    model.scrollTo(200);
    model.grow("3.0", 1300);
    expect(model.top()).toBe(200);
    model.scrollTo(model.naturalEnd());
    model.grow("3.0", 1800);
    expect(model.top()).toBe(model.naturalEnd());
    view.unmount();
  });

  it("mobile coarse-pointer slight upward touch drag leaves tail without pullback while streaming grows", () => {
    const browser = fakeBrowser({ media: { "(pointer: coarse)": true } });
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} />, { browser });
    const tail = model.naturalEnd();
    expect(model.top()).toBe(tail);

    model.touchStart(500);
    model.touchMove(508);
    model.scrollTo(tail - 8);
    model.touchEnd();
    model.writes.length = 0;

    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    model.grow("3.0", 800);
    expect(model.top()).toBe(tail - 8);
    expect(model.writes).toEqual([]);

    model.scrollTo(model.naturalEnd());
    model.grow("3.0", 1200);
    expect(model.top()).toBe(model.naturalEnd());
    view.unmount();
  });
});

describe("bottom overlay slack (grown composer and pending strip)", () => {
  it("growing bottomOverlayHeight at the tail moves no messages and opens manual scroll slack", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    const restingTail = model.naturalEnd();
    const restingRowTop = model.rowTop("2.0");
    expect(model.top()).toBe(restingTail);

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={180} />);
    expect(model.top()).toBe(restingTail);
    expect(model.rowTop("2.0")).toBe(restingRowTop);
    expect(model.naturalEnd()).toBe(restingTail);
    expect(model.max()).toBe(restingTail + 180);
    expect(arrow()).toBeNull();
    view.unmount();
  });

  it("holds a reader's manual scroll into overlay slack across commits and resumes tail following when the overlay collapses", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} bottomOverlayHeight={180} />);
    const restingTail = model.naturalEnd();

    model.scrollTo(restingTail + 180);
    expect(model.top()).toBe(restingTail + 180);
    expect(arrow()).toBeNull();

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={180} />);
    expect(model.top()).toBe(restingTail + 180);

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={100} />);
    expect(model.top()).toBe(restingTail + 100);
    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={100} />);
    expect(model.top()).toBe(restingTail + 100);

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    expect(model.top()).toBe(restingTail);
    view.rerender(<TimelineView items={[earlier, sent, reply]} bottomOverlayHeight={0} />);
    model.grow("3.0", 500);
    expect(model.top()).toBe(model.naturalEnd());
    view.unmount();
  });

  it("leaves a reader in history untouched and lifts the down arrow when bottomOverlayHeight grows", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    model.scrollTo(120);
    const historyRowTop = model.rowTop("1.0");

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={140} />);
    expect(model.top()).toBe(120);
    expect(model.rowTop("1.0")).toBe(historyRowTop);
    const downArrow = arrow();
    expect(downArrow).not.toBeNull();
    expect(downArrow!.parentElement!.style.getPropertyValue("--timeline-footer-h")).toBe("140px");
    view.unmount();
  });

  it("does not switch a history reader to tail when the viewport grows and clamps scrollTop with no overlay", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    model.scrollTo(450);
    expect(model.top()).toBe(450);

    model.setViewportHeight(800);
    model.resize();
    expect(model.top()).toBe(model.naturalEnd());

    view.rerender(<TimelineView items={[earlier, sent, reply]} bottomOverlayHeight={0} />);
    model.grow("3.0", 500);
    expect(model.top()).not.toBe(model.naturalEnd());
    view.unmount();
  });

  it("resumes tail following when overlay collapses in the same commit that appends a delivered message", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} bottomOverlayHeight={60} />);
    const initialEnd = model.naturalEnd();

    model.scrollTo(initialEnd + 60);
    expect(model.top()).toBe(initialEnd + 60);

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    expect(model.top()).toBe(model.naturalEnd());

    view.rerender(<TimelineView items={[earlier, sent, reply]} bottomOverlayHeight={0} />);
    model.grow("3.0", 600);
    expect(model.top()).toBe(model.naturalEnd());
    view.unmount();
  });

  it("keeps short conversations still when overlay grows, allows manual scroll, and returns cleanly on collapse", () => {
    const model = timelineLayout({ viewportHeight: 600, rowHeight: 200 });
    const view = render(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    expect(model.naturalEnd()).toBe(0);
    expect(model.top()).toBe(0);
    const restingRowTop = model.rowTop("2.0");

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={250} />);
    expect(model.top()).toBe(0);
    expect(model.rowTop("2.0")).toBe(restingRowTop);
    expect(model.max()).toBeGreaterThan(0);

    const maxSlack = model.max();
    model.scrollTo(maxSlack);
    expect(model.top()).toBe(maxSlack);

    view.rerender(<TimelineView items={[earlier, sent]} bottomOverlayHeight={0} />);
    expect(model.top()).toBe(0);
    view.unmount();
  });

  it("collapses a turn to the natural end of the log with a grown overlay", () => {
    const workItems = [
      earlier,
      makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "step 1" }),
      makeItem({ item_id: "2.1", kind: "agent_reasoning", payload: "step 2" }),
      makeItem({ item_id: "2.2", kind: "agent_reasoning", payload: "step 3" }),
      makeItem({ item_id: "2.3", kind: "agent_chat", payload: "done" }),
    ];
    const heights: Record<string, number> = { "1.0": 1000, "2.0": 200, "2.1": 200, "2.2": 200, "2.3": 80 };
    const model = timelineLayout({ viewportHeight: 600, rowHeight: (row) => heights[row.dataset.itemId ?? ""] ?? 80 });
    const view = render(<TimelineView items={workItems} bottomOverlayHeight={100} />);
    const toggle = screen.getByTestId("turn-toggle");
    act(() => { toggle.click(); });
    model.scrollTo(model.naturalEnd() - 200);
    act(() => { toggle.click(); });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.max()).toBe(model.naturalEnd() + 100);
    expect(arrow()).toBeNull();

    view.rerender(<TimelineView items={workItems} bottomOverlayHeight={0} />);
    expect(model.top()).toBe(model.naturalEnd());
    expect(model.max()).toBe(model.naturalEnd());
    view.unmount();
  });
});
