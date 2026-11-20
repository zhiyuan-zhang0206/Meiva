import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import {
  ChatMarkdownMock,
  ScrollAreaMock,
  cleanupTimelineTest,
  displayLimits,
  makeItem,
  render,
  timelineLayout,
  timelineQueryClient,
} from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useTimelineStore.getState().switchThread(null, [], false);
  cleanupTimelineTest();
});

const chat = (id: string, payload = id) => makeItem({ item_id: id, kind: "agent_chat", payload });
const arrow = () => screen.queryByTestId("scroll-to-bottom");

// meiva:note
// Nothing to scroll to means nothing to offer: at the bottom the control is not
// in the DOM, so a hover cannot turn it into an arrow over a click that does
// nothing (user report 2026-10-30). A refresh in flight does not override it.
// The state is the measured distance to the bottom, so appended content
// re-decides it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
describe("scroll-to-bottom control presence", () => {
  it("is absent at the bottom, also inside the bottom zone", () => {
    const layout = timelineLayout({ rowHeight: 5000 });
    render(<TimelineView items={[chat("10.0")]} />);
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(arrow()).toBeNull();
    layout.scrollTo(layout.naturalEnd() - 10);
    expect(arrow()).toBeNull();
  });

  it("appears away from the bottom, rides smoothly to the tail, and follows once it arrives", () => {
    const layout = timelineLayout({ rowHeight: 5000 });
    const view = render(<TimelineView items={[chat("10.0")]} />);
    layout.scrollTo(700);
    const button = arrow()!;
    expect(button.getAttribute("aria-label")).toBe("Scroll down");
    expect(button.className).not.toContain("opacity-0");
    expect(button.className).not.toContain("pointer-events-none");

    fireEvent.click(button);
    expect(layout.smoothTarget()).toBe(layout.naturalEnd());
    expect(layout.top()).toBe(700);
    expect(arrow()).toBeNull();

    layout.finishSmooth();
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(arrow()).toBeNull();
    view.rerender(<TimelineView items={[chat("10.0"), chat("11.0")]} />);
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(arrow()).toBeNull();
  });

  it("reaches the true bottom in one click across multi-wave virtualized height shifts", () => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_MEASURE_ROWS", 5);
    // meiva:note
    // The rows open at the window's 80px estimate and land at their real heights while the reader
    // is far above them, so the end the ride heads for moves as they mount and are measured.
    let landed = false;
    const layout = timelineLayout({
      rowHeight: (row) => !landed ? 80 : Number.parseInt(row.dataset.itemId ?? "1") % 3 === 0 ? 120 : 60,
    });
    const rows = Array.from({ length: 80 }, (_, i) => chat(`${i + 1}.0`, `Reply ${i + 1}`));
    render(<TimelineView items={rows} threadKey="ride-virtual-waves" />);
    layout.resize();
    while (layout.top() > 400) layout.scrollTo(Math.max(400, layout.top() - 300));
    landed = true;
    layout.resize();
    expect(layout.viewport().querySelector('[data-timeline-spacer="after"]')).not.toBeNull();

    fireEvent.click(arrow()!);
    const target = layout.smoothTarget()!;
    expect(target).toBeGreaterThan(2000);
    layout.finishSmooth();
    for (let wave = 0; wave < 4; wave += 1) {
      layout.resize();
      layout.echo();
    }

    expect(layout.viewport().querySelector('[data-timeline-spacer="after"]')).toBeNull();
    expect(layout.viewport().querySelector('.timeline-item[data-item-id="80.0"]')).not.toBeNull();
    expect(layout.top()).not.toBe(target);
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.max() - layout.top()).toBeLessThanOrEqual(1);
    expect(arrow()).toBeNull();
  });

  // meiva:note
  // Suspected product bug: when the mounted rows are much taller than the window's 80px estimate,
  // the writer's tail pin and the window's range feed each other synchronously (each write moves
  // the range, each range moves the natural end) and React aborts with "Maximum update depth
  // exceeded" before any ResizeObserver can deliver real heights. 300 rows of 200px with the
  // default limits reproduce it on mount; the same loop hits the ride above with 36/340px rows.
  it("opens a virtualized conversation at the tail when its rows are much taller than estimated", () => {
    const layout = timelineLayout({ rowHeight: 200 });
    render(<TimelineView items={Array.from({ length: 300 }, (_, i) => chat(`${i + 1}.0`))} threadKey="tall-rows" />);
    expect(layout.top()).toBe(layout.naturalEnd());
  });

  // meiva:note
  // Suspected product bug: a reader scroll that lands beyond the mounted rows in one event (a
  // scrollbar drag, Home, a long fling sample) reads no row back, so the anchor stays on the tail;
  // the window then mounts rows for the new position and the writer pins the tail again.
  it("leaves the tail for a scrollbar jump into unmounted history", () => {
    const layout = timelineLayout({ rowHeight: 80 });
    render(<TimelineView items={Array.from({ length: 300 }, (_, i) => chat(`${i + 1}.0`))} threadKey="jump" />);
    layout.resize();
    layout.scrollTo(3000);
    expect(layout.top()).toBe(3000);
    expect(arrow()).toBeTruthy();
  });

  it("drives to the true bottom in one click even when an expanded live work block grows", () => {
    timelineQueryClient.setQueryData(SETTINGS_QUERY_KEY, {
      ...USER_SETTING_DEFAULTS,
      "display.expand_working_block": true,
    });
    const layout = timelineLayout({ rowHeight: 300 });
    const step = (i: number) => makeItem({ item_id: `2.${i}`, kind: "agent_reasoning", payload: `Step ${i + 1}` });
    const live = (count: number): BackendTimelineItem[] => [
      makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Run diagnostics" }),
      ...Array.from({ length: count }, (_, i) => step(i)),
    ];
    const view = render(<TimelineView items={live(15)} threadKey="live-block-ride" turnActive />);
    expect(layout.top()).toBe(layout.naturalEnd());

    // meiva:note
    // The reader leaves the tail (the human message is above the viewport, so the arrow rides).
    layout.wheel(-1200);
    const reading = layout.top();
    view.rerender(<TimelineView items={live(20)} threadKey="live-block-ride" turnActive />);
    layout.resize();
    expect(layout.top()).toBe(reading);

    fireEvent.click(arrow()!);
    const target = layout.smoothTarget()!;
    expect(target).toBe(layout.naturalEnd());
    // meiva:note
    // The block keeps writing while the ride is in flight.
    view.rerender(<TimelineView items={live(30)} threadKey="live-block-ride" turnActive />);
    layout.finishSmooth();
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.top()).toBeGreaterThan(target);

    view.rerender(<TimelineView items={live(40)} threadKey="live-block-ride" turnActive />);
    layout.resize();
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(arrow()).toBeNull();
  });

  it("aborts the scroll-to-bottom ride immediately when the user wheels upward", () => {
    const layout = timelineLayout({ rowHeight: 5000 });
    render(<TimelineView items={[chat("10.0")]} threadKey="wheel-abort-ride" />);
    layout.scrollTo(700);
    fireEvent.click(arrow()!);
    expect(layout.smoothTarget()).not.toBeNull();

    // meiva:note
    // Mid-ride: the position has advanced, then an upward notch.
    layout.drift(1800);
    layout.echo();
    layout.wheel(-80);
    expect(layout.smoothTarget()).toBeNull();
    act(() => { layout.viewport().dispatchEvent(new Event("scrollend")); });
    layout.resize();

    expect(layout.top()).toBe(1720);
    expect(arrow()).toBeTruthy();
  });
});

// meiva:note
// The arrow goes to the latest human message while it sits below the reading offset, then to the
// tail.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
describe("the arrow's target", () => {
  it("rides to the tail and follows it, wherever the reader is", () => {
    const layout = timelineLayout({ rowHeight: (row) => row.dataset.itemId === "3.0" ? 2000 : 200 });
    render(<TimelineView items={[
      chat("1.0"),
      makeItem({ item_id: "2.0", kind: "inbound_chat", source: "user", payload: "My question" }),
      chat("3.0", "A long answer"),
    ]} />);
    layout.scrollTo(0);
    fireEvent.click(arrow()!);
    expect(layout.smoothTarget()).toBe(layout.naturalEnd());
    layout.finishSmooth();
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(arrow()).toBeNull();
  });
});

// meiva:note
// The pending strip floats over the timeline's bottom edge: its height reaches the log's padding and
// the arrow through one variable on the timeline wrapper.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
describe("the footer", () => {
  it("floats over the bottom edge and publishes its height on the timeline wrapper", () => {
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get(this: HTMLElement) { return this.hasAttribute("data-timeline-footer") ? 40 : 0; },
    });
    try {
      render(<TimelineView items={[chat("1.0")]} footer={<div data-testid="footer-probe" />} />);
      const footer = screen.getByTestId("footer-probe").closest<HTMLElement>("[data-timeline-footer]");
      expect(footer).not.toBeNull();
      expect(footer!.parentElement!.style.getPropertyValue("--timeline-footer-h")).toBe("40px");
    } finally {
      if (original) Object.defineProperty(HTMLElement.prototype, "offsetHeight", original);
      else Reflect.deleteProperty(HTMLElement.prototype, "offsetHeight");
    }
  });
});

// meiva:note
// Only a following reader releases the oldest rows; the arrow makes the reader follow again.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
describe("follower trimming", () => {
  function readerAboveTrimmableHistory() {
    const layout = timelineLayout({ rowHeight: 400 });
    const fullItems = Array.from({ length: 12 }, (_, i) => chat(`${i + 1}.0`, `Msg ${i + 1}`));
    useTimelineStore.getState().switchThread(99, fullItems.slice(6), true);
    function Selected() {
      const items = useTimelineStore((s) => s.items);
      return <TimelineView items={items} threadKey="99" retainedItemsMax={6} />;
    }
    render(<Selected />);
    layout.wheel(-1200);
    act(() => {
      useTimelineStore.getState().beginLoadOlder();
      useTimelineStore.getState().prependOlder(fullItems.slice(0, 6), false);
    });
    expect(useTimelineStore.getState().items).toHaveLength(12);
    return layout;
  }

  it("keeps a reader's history while they read, and trims it once the arrow's ride arrives", () => {
    const layout = readerAboveTrimmableHistory();
    fireEvent.click(arrow()!);
    layout.finishSmooth();
    expect(useTimelineStore.getState().items.length).toBeLessThanOrEqual(7);
    expect(layout.top()).toBe(layout.naturalEnd());
    expect(layout.viewport().querySelector('.timeline-item[data-item-id="12.0"]')).not.toBeNull();
  });

  // meiva:note
  // Suspected regression: the arrow sets the tail anchor at the start of the ride, so the next
  // commit trims the oldest rows while the ride is still in flight. The rows above the reader
  // vanish under the ride (2472px here) and the smooth ride becomes a jump to the clamped end.
  it("does not trim the reader's history before the ride arrives", () => {
    const layout = readerAboveTrimmableHistory();
    fireEvent.click(arrow()!);
    expect(layout.smoothTarget()).not.toBeNull();
    expect(useTimelineStore.getState().items).toHaveLength(12);
  });
});

// meiva:note
// Manual expansion keeps the clicked header where it is and releases tail following, in every
// mode: live or settled, following or reading, and during the arrow's ride, which it cancels
// (user ruling 2026-11-17). Later growth keeps it there too.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/live-block.md
describe("header toggle", () => {
  it.each([
    [true, true, false], [true, false, false], [false, true, false],
    [false, false, false], [true, false, true], [false, false, true],
  ])("keeps the clicked header in place on expansion (live=%s, following=%s, ride=%s)", (turnActive, following, ride) => {
    displayLimits.set("MEIVA_TIMELINE_WINDOW_ACTIVATION_ROWS", 20);
    displayLimits.set("MEIVA_TIMELINE_WINDOW_TURN_ROWS", 20);
    // meiva:note
    // A tall reply above the collapsed turn gives the timeline its scroll range.
    const layout = timelineLayout({ rowHeight: (row) => row.dataset.itemId === "1.0" ? 1500 : 80 });
    const workItems = [
      chat("1.0", "Progress"),
      ...Array.from({ length: 40 }, (_, i) => makeItem({ item_id: `2.${i}`, kind: "agent_reasoning", payload: `Step ${i + 1}` })),
    ];
    const view = render(<TimelineView items={workItems} turnActive={turnActive} />);
    // meiva:note
    // The browser measures the mounted rows before the reader can click.
    layout.resize();
    const button = screen.getByTestId("turn-toggle");
    expect(layout.top()).toBe(layout.naturalEnd());
    // meiva:note
    // A reader 50px above the end still sees the header, outside the 30px bottom zone.
    if (!following) layout.wheel(-50);
    if (ride) {
      fireEvent.click(arrow()!);
      expect(layout.smoothTarget()).toBe(layout.naturalEnd());
    }
    const clickedTop = button.getBoundingClientRect().top;
    expect(clickedTop).toBeGreaterThan(0);
    expect(clickedTop).toBeLessThan(600);
    const initialTop = layout.top();

    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(layout.smoothTarget()).toBeNull();
    expect(button.getBoundingClientRect().top).toBe(clickedTop);
    expect(layout.top()).toBe(initialTop);
    expect(arrow()).toBeTruthy();

    view.rerender(<TimelineView items={[...workItems, makeItem({ item_id: "2.40", kind: "agent_reasoning", payload: "New step" })]} turnActive={turnActive} />);
    layout.resize();
    layout.echo();
    expect(layout.top()).toBe(initialTop);
    expect(button.getBoundingClientRect().top).toBe(clickedTop);
  });
});
