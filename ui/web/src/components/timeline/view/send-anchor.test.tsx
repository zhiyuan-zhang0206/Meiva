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
    expect(model.reserve()).toBe(0);
    expect(arrow()).toBeNull();
  });

  it("leaves a reader in history where they are, and the arrow takes them to the tail", () => {
    const model = layout();
    const view = render(<TimelineView items={[earlier]} />);
    model.scrollTo(100);
    view.rerender(<TimelineView items={[earlier, sent, reply]} />);
    model.grow("3.0", 800);
    expect(model.top()).toBe(100);
    expect(model.reserve()).toBe(0);
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
    model.scrollTo(model.naturalEnd() - 10);
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

    model.scrollTo(model.naturalEnd() - 40);
    model.grow("3.0", 1200);
    expect(model.top()).toBe(model.naturalEnd());
    view.unmount();
  });
});
