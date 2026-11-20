// meiva:note
// timeline.tsx component test — ItemView per-kind render + envelope split + system_marker dispatch + LifecycleChip + UnknownMarkerChip + InterruptedNotice + CopyButton. The test DOM lays nothing out, so the scroll tests below run on the `timelineLayout` model (view/timeline-test-mock.tsx). ChatMarkdown / PythonCode / ScrollArea are mocked as stubs to reduce noise and avoid the runtime complexity of react-markdown / Prism / base-ui.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md

import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { fakeBrowser } from "@/lib/browser/testing";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, historyWheel, makeItem, pullOlderAtTop, render, settingsState, timelineLayout } from "./view/timeline-test-mock";

vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from ".";

afterEach(cleanupTimelineTest);
describe("TimelineView accessibility", () => {
  it("is a polite log and announces turn boundaries without streaming chunks", () => {
    const { rerender } = render(<TimelineView items={[]} />);
    const log = screen.getByRole("log");
    expect(log.getAttribute("aria-live")).toBe("polite");
    expect(log.getAttribute("aria-relevant")).toBe("additions");
    expect(screen.queryByTestId("timeline-turn-announcement")).toBeNull();

    rerender(<TimelineView items={[]} turnActive />);
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent is responding.");

    rerender(
      <TimelineView
        turnActive
        items={[makeItem({ item_id: "streaming", kind: "agent_chat", payload: "A streaming chunk" })]}
      />,
    );
    expect(log.querySelector('[data-item-id="streaming"]')?.getAttribute("aria-live")).toBe("off");
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent is responding.");

    rerender(<TimelineView items={[]} />);
    expect(screen.getByTestId("timeline-turn-announcement").textContent).toBe("Agent response complete.");
  });
});

describe("timeline document work", () => {
  it("skips its render when the page re-renders with identical props", () => {
    const items = [makeItem({ kind: "agent_chat", payload: "steady" })];
    const onLoadOlder = vi.fn();
    const { rerender } = render(<TimelineView items={items} threadKey="memo" onLoadOlder={onLoadOlder} />);
    const reads = settingsState.reads;
    // meiva:note
    // Parent-only re-renders (e.g. composer upload progress) must not re-run TimelineView.
    rerender(<TimelineView items={items} threadKey="memo" onLoadOlder={onLoadOlder} />);
    expect(settingsState.reads).toBe(reads);
    rerender(<TimelineView items={[...items]} threadKey="memo" onLoadOlder={onLoadOlder} />);
    expect(settingsState.reads).toBeGreaterThan(reads);
  });

  it("reuses turn summaries for view changes and refreshes them when items change", () => {
    const code = makeItem({
      item_id: "1.0",
      kind: "agent_code",
      payload: "meiva.files.read('first')",
      sdk_calls: [{ method: "files.read", count: 1 }],
    });
    // meiva:note
    // Reading committed SDK calls is part of deriving the turn summary.
    // Expanding/collapsing and parent-only updates must not re-read historical data.
    const calls = vi.fn(() => [{ method: "files.read", count: 1 }]);
    Object.defineProperty(code, "sdk_calls", { get: calls });
    const items = [code];
    const { rerender } = render(<TimelineView items={items} threadKey="first" />);
    expect(calls).toHaveBeenCalled();
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();
    calls.mockClear();
    rerender(<TimelineView items={items} threadKey="first" />);
    expect(calls).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.getByTestId("python-code").textContent).toBe("meiva.files.read('first')");
    calls.mockClear();
    rerender(<TimelineView items={items} threadKey="first" />);
    expect(calls).not.toHaveBeenCalled();

    const next = makeItem({
      ...code,
      payload: "meiva.files.read('updated')",
      sdk_calls: [{ method: "files.read", count: 2 }],
    });
    rerender(<TimelineView items={[next]} threadKey="first" />);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("files.read × 2");
    expect(screen.getByTestId("python-code").textContent).toBe("meiva.files.read('updated')");

    rerender(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "another conversation" })]}
        threadKey="second"
      />,
    );
    expect(screen.queryByTestId("turn-toggle")).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("another conversation");
  });
});

describe("CopyButton click behavior", () => {
  it("click copy → asks the page to copy the chat text", async () => {
    const browser = fakeBrowser();
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "to copy" })]}
      />,
      { browser },
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(browser.actions.copied).toEqual(["to copy"]));
  });

  it("shows the Copied feedback once the page accepted the copy", async () => {
    const browser = fakeBrowser();
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "fallback text" })]}
      />,
      { browser },
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
  });
});

describe("touch drag vs streaming pin (#1016)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // meiva:note
  // One reply that streams taller; the viewport is 600px and the mouse bottom zone 30px.
  function streamingReply() {
    let height = 1000;
    const layout = timelineLayout({ rowHeight: () => height });
    render(<TimelineView items={[makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })]} />);
    return { layout, grow: (by: number) => { height += by; layout.resize(); } };
  }

  it("a layout change during an active touch drag does not pin; release beyond the zone stops following", () => {
    const { layout, grow } = streamingReply();
    expect(layout.top()).toBe(layout.naturalEnd());

    layout.touchStart();
    const dragged = layout.naturalEnd() - 20;
    layout.scrollTo(dragged);
    expect(layout.top()).toBe(dragged);
    // meiva:note
    // A chunk lands mid-drag: the viewport stays under the finger.
    grow(150);
    expect(layout.top()).toBe(dragged);
    grow(150);
    expect(layout.top()).toBe(dragged);

    // meiva:note
    // The finger lifts beyond the zone: following stops and the next chunk leaves the reader.
    layout.touchEnd();
    grow(150);
    expect(layout.top()).toBe(dragged);
  });

  it("the pin returns once the user drags back to the bottom and releases there", () => {
    const { layout, grow } = streamingReply();
    layout.touchStart();
    layout.scrollTo(200);
    layout.touchEnd();
    grow(200);
    expect(layout.top()).toBe(200);

    layout.touchStart();
    layout.scrollTo(layout.naturalEnd());
    layout.touchEnd();
    grow(200);
    expect(layout.top()).toBe(layout.naturalEnd());
  });
});

describe("bounded timeline history (#4702)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useTimelineStore.getState().switchThread(null, [], false);
  });

  it("fills a short cold viewport but layout echoes never walk more than two pages", () => {
    const layout = timelineLayout();
    const loadOlder = vi.fn();
    const item = (id: number) => makeItem({ item_id: `${id}.0`, kind: "agent_chat", payload: `row ${id}` });
    const { rerender } = render(<TimelineView threadKey="42" items={[item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    expect(layout.scrollHeight()).toBe(600);
    expect(loadOlder).toHaveBeenCalledTimes(1); // first screen still fills
    // meiva:note
    // The fill reads history the reader never asked for, so it must not present
    // the loading strip above the messages (user report 2026-10-29).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
    expect(loadOlder).toHaveBeenCalledWith("background");
    for (let i = 0; i < 8; i++) layout.resize();
    expect(loadOlder).toHaveBeenCalledTimes(1);

    rerender(<TimelineView threadKey="42" items={[item(30)]}
      hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView threadKey="42" items={[item(20), item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    layout.resize();
    expect(loadOlder).toHaveBeenCalledTimes(2);
    rerender(<TimelineView threadKey="42" items={[item(20), item(30)]}
      hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView threadKey="42" items={[item(10), item(20), item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    expect(layout.scrollHeight()).toBe(600);
    for (let i = 0; i < 8; i++) layout.resize();
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });

  it("does not load from layout changes when the viewport already scrolls", () => {
    const layout = timelineLayout({ rowHeight: 1200 });
    const loadOlder = vi.fn();
    render(<TimelineView threadKey="42" items={[makeItem({ item_id: "30.0", kind: "agent_chat", payload: "tail" })]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    expect(layout.scrollHeight()).toBeGreaterThan(600);
    for (let i = 0; i < 10; i++) layout.resize();
    expect(loadOlder).not.toHaveBeenCalled();
  });

  it("trims many fetched pages and streamed rows while following, preserving older paging", () => {
    const rows = (first: number, last: number) => Array.from(
      { length: last - first + 1 },
      (_, i) => makeItem({ item_id: `${first + i}.0`, kind: "agent_chat", payload: `row ${first + i}` }),
    );
    useTimelineStore.getState().switchThread(42, rows(251, 300), true);
    function SelectedTimeline() {
      const items = useTimelineStore((s) => s.items);
      return <TimelineView threadKey="42" items={items} retainedItemsMax={250} />;
    }
    const { container } = render(<SelectedTimeline />);
    for (let page = 4; page >= 0; page--) {
      act(() => useTimelineStore.getState().prependOlder(rows(page * 50 + 1, page * 50 + 50), true));
    }
    act(() => useTimelineStore.setState((s) => ({ items: [...s.items, ...rows(301, 350)] })));
    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(250);
    expect(state.hasMoreOlder).toBe(true);
    expect(state.items.at(-1)?.item_id).toBe("350.0");
    expect(container.querySelectorAll(".timeline-item").length).toBeLessThanOrEqual(250);
  });

  it("keeps a parked reader's history until they return to the bottom", () => {
    const layout = timelineLayout();
    const rows = (first: number, last: number) => Array.from(
      { length: last - first + 1 },
      (_, i) => makeItem({ item_id: `${first + i}.0`, kind: "agent_chat", payload: `row ${first + i}` }),
    );
    useTimelineStore.getState().switchThread(42, rows(201, 210), true);
    function SelectedTimeline() {
      const items = useTimelineStore((s) => s.items);
      return <TimelineView threadKey="42" items={items} retainedItemsMax={200} />;
    }
    const { container } = render(<SelectedTimeline />);
    expect(layout.top()).toBe(layout.naturalEnd());
    layout.scrollTo(100);
    act(() => useTimelineStore.getState().prependOlder(rows(1, 200), true));
    expect(useTimelineStore.getState().items).toHaveLength(210);
    expect(container.querySelectorAll(".timeline-item").length).toBeLessThan(80);
    expect(container.querySelector("[data-timeline-spacer]")).not.toBeNull();
    layout.scrollTo(layout.max());
    expect(useTimelineStore.getState().items).toHaveLength(200);
  });

  it("loads one page per upward gesture and ignores compensation and momentum after the page lands", () => {
    const layout = timelineLayout({ rowHeight: (row) => row.dataset.itemId === "90.0" ? 300 : 1200 });
    const loadOlder = vi.fn();
    const item = (id: number) => makeItem({ item_id: `${id}.0`, kind: "agent_chat", payload: `row ${id}` });
    const items = [item(100)];
    const { rerender } = render(
      <TimelineView threadKey="42" items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = layout.viewport();
    layout.scrollTo(500);
    layout.scrollTo(0);
    expect(loadOlder).not.toHaveBeenCalled();
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledOnce();

    const readingTop = layout.rowTop("100.0");
    rerender(<TimelineView threadKey="42" items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView threadKey="42" items={[item(90), ...items]} hasMoreOlder onLoadOlder={loadOlder} />);
    // meiva:note
    // The landed page sits above the reading row, which keeps its place: 300px plus the row gap.
    expect(layout.rowTop("100.0")).toBe(readingTop);
    expect(layout.top()).toBe(312);
    layout.echo();
    for (let i = 0; i < 6; i++) {
      expect(historyWheel(viewport, -100, true).defaultPrevented).toBe(true);
    }
    expect(layout.top()).toBe(312);
    expect(loadOlder).toHaveBeenCalledOnce();

    historyWheel(viewport, 40, false);
    layout.scrollTo(0);
    historyWheel(viewport, -100, true);
    expect(loadOlder).toHaveBeenCalledOnce();
    historyWheel(viewport, 40, false);
    layout.scrollTo(0);
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });
});

// meiva:note
// The gap above the composer's rule is the list's share (`timeline-log`: 1.5rem, or what a taller
// footer needs) plus the composer's (pt-0.5), and the list's share only reaches the rule because the
// log keeps a trailing element to hold it — the last row overflows the log instead when that element
// goes.
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.md
describe("the gap above the composer rule", () => {
  it("keeps the list's share of the gap in timeline-log, and a trailing element to hold it", () => {
    render(<TimelineView items={[
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "one" }),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "two" }),
    ]} />);
    const log = screen.getByRole("log");
    expect(log.className).toContain("timeline-log");
    expect(log.className).not.toMatch(/\bpb-/);
    expect(log.className).toContain("space-y-3");
    expect(log.lastElementChild?.getAttribute("data-virtual-group")).toBeNull();
  });
});
