// meiva:note
// timeline.tsx component test — ItemView per-kind render + envelope split + system_marker dispatch + LifecycleChip + UnknownMarkerChip + InterruptedNotice + CopyButton. jsdom does not actually render scroll, so scroll behavior is not tested (the sticky state machine was extracted to lib/sticky.ts and is covered by sticky.test.ts). ChatMarkdown / PythonCode / ScrollArea are mocked as stubs to reduce noise and avoid the runtime complexity of react-markdown / Prism / base-ui.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md

import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { fakeBrowser } from "@/lib/browser/testing";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, historyWheel, makeItem, pullOlderAtTop, render, toggleState } from "./view/timeline-test-mock";

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
    const reads = toggleState.reads;
    // e.g. the composer's upload progress or the token meter updated.
    rerender(<TimelineView items={items} threadKey="memo" onLoadOlder={onLoadOlder} />);
    expect(toggleState.reads).toBe(reads);
    rerender(<TimelineView items={[...items]} threadKey="memo" onLoadOlder={onLoadOlder} />);
    expect(toggleState.reads).toBeGreaterThan(reads);
  });

  it("reuses turn summaries for view changes and refreshes them when items change", () => {
    const code = makeItem({
      item_id: "1.0",
      kind: "agent_code",
      payload: "meiva.files.read('first')",
      sdk_calls: [{ method: "files.read", count: 1 }],
    });
    // Reading committed SDK calls is part of deriving the turn summary.
    // A collapse and a parent-only update must not re-read historical data.
    const calls = vi.fn(() => [{ method: "files.read", count: 1 }]);
    Object.defineProperty(code, "sdk_calls", { get: calls });
    const items = [code];
    const { rerender } = render(<TimelineView items={items} threadKey="first" />);
    expect(calls).toHaveBeenCalled();
    calls.mockClear();

    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();
    rerender(<TimelineView items={items} threadKey="first" />);
    expect(calls).not.toHaveBeenCalled();

    const next = makeItem({
      ...code,
      payload: "meiva.files.read('updated')",
      sdk_calls: [{ method: "files.read", count: 2 }],
    });
    rerender(<TimelineView items={[next]} threadKey="first" />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
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

// Details mode ("all" / "last" / "none") controls the default expanded
// state for every block across all message kinds. Secondary items (thinking,
// code, output, system prompts) are grouped into turn blocks — the turn header
// shows an aggregate summary; individual cards are only visible when the turn

describe("touch drag vs streaming pin (#1016)", () => {
  // happy-dom's ResizeObserver never fires; drive the component's RO
  // manually so the pin path is exercised deterministically.
  let roCallback: ResizeObserverCallback | null = null;
  let roObserved: Element[] = [];

  beforeEach(() => {
    roCallback = null;
    roObserved = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: ResizeObserverCallback) {
          roCallback = cb;
        }
        observe(el: Element) {
          roObserved.push(el);
        }
        /* eslint-disable @typescript-eslint/no-empty-function -- observer teardown is a no-op in the manual stub */
        unobserve() {}
        disconnect() {}
        /* eslint-enable @typescript-eslint/no-empty-function */
        takeRecords() {
          return [];
        }
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // Drive one RO callback with the given geometry (the component's callback
  // reads the live viewport, so defineProperty the numbers first).
  const fireRO = () => {
    expect(roCallback).not.toBeNull();
    roCallback!([], {} as ResizeObserver);
  };

  const viewportGeom = (viewport: HTMLElement, scrollHeight: number, clientHeight: number) => {
    Object.defineProperty(viewport, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: clientHeight, configurable: true });
  };

  it("a layout change during an active touch drag does not pin; release beyond the zone stops following", () => {
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    // Viewport 600 tall, content 1000, user pinned at the bottom (400).
    viewportGeom(viewport, 1000, 600);
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll")); // position rule → sticky
    fireRO(); // initial RO → pin (already at bottom, no-op)

    // The user starts dragging up.
    fireEvent.touchStart(viewport, { touches: [{ clientY: 100 }] });
    viewport.scrollTop = 370;
    viewport.dispatchEvent(new Event("scroll")); // 30px up — inside the zone
    expect(viewport.scrollTop).toBe(370); // drag is honored

    // A chunk lands mid-drag (content grows to 1150): with the fix the RO
    // must NOT pin the viewport back to the bottom under the user's finger.
    viewportGeom(viewport, 1150, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);

    // Another chunk while still dragging — still no pin.
    viewportGeom(viewport, 1300, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);

    // The finger lifts beyond the zone (dist = 1300 - 370 - 600 = 330) —
    // following stops; the next chunk must not yank the reader back.
    viewport.dispatchEvent(new Event("touchend"));
    viewportGeom(viewport, 1450, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(370);
  });

  it("the pin returns once the user drags back to the bottom and releases there", () => {
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    viewportGeom(viewport, 1000, 600);
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll"));

    fireEvent.touchStart(viewport, { touches: [{ clientY: 100 }] });
    viewport.scrollTop = 200; // dragged far up
    viewport.dispatchEvent(new Event("scroll")); // position rule unsticks
    viewport.dispatchEvent(new Event("touchend"));

    // Back to the bottom (re-stick via the position rule).
    viewport.scrollTop = 400;
    viewport.dispatchEvent(new Event("scroll"));
    // Growth now pins again. (happy-dom does not clamp scrollTop on
    // assignment; a real browser clamps the pin's scrollTop =
    // scrollHeight write to scrollHeight - clientHeight = 600. The
    // assertion is on the pin HAVING happened — scrollTop moved from
    // 400 to the written scrollHeight.)
    viewportGeom(viewport, 1200, 600);
    fireRO();
    expect(viewport.scrollTop).toBe(1200); // pinned (real browser: 600)
  });
});

describe("bounded timeline history (#4702)", () => {
  let roCallback: ResizeObserverCallback | null = null;

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { roCallback = cb; }
      /* eslint-disable @typescript-eslint/no-empty-function -- manual observer stub */
      observe() {}
      unobserve() {}
      disconnect() {}
      /* eslint-enable @typescript-eslint/no-empty-function */
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useTimelineStore.getState().switchThread(null, [], false);
  });

  function geometry(viewport: HTMLElement, scrollHeight: number, clientHeight = 600) {
    Object.defineProperty(viewport, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: clientHeight, configurable: true });
  }
  const rowRect = (top: number): DOMRect =>
    ({ top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) });
  function layoutChange() {
    act(() => roCallback!([], {} as ResizeObserver));
  }

  it("fills a short cold viewport but layout echoes never walk more than two pages", () => {
    const loadOlder = vi.fn();
    const item = (id: number) => makeItem({ item_id: `${id}.0`, kind: "agent_chat", payload: `row ${id}` });
    const { rerender } = render(<TimelineView threadKey="42" items={[item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport, 300);
    layoutChange();
    expect(loadOlder).toHaveBeenCalledTimes(1); // first screen still fills
    // meiva:note
    // The fill reads history the reader never asked for, so it must not present
    // the loading strip above the messages (user report 2026-10-29).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.meiva.okf.md
    expect(loadOlder).toHaveBeenCalledWith("background");
    for (let i = 0; i < 8; i++) layoutChange();
    expect(loadOlder).toHaveBeenCalledTimes(1);

    rerender(<TimelineView threadKey="42" items={[item(30)]}
      hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView threadKey="42" items={[item(20), item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    geometry(viewport, 450);
    layoutChange();
    expect(loadOlder).toHaveBeenCalledTimes(2);
    rerender(<TimelineView threadKey="42" items={[item(20), item(30)]}
      hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView threadKey="42" items={[item(10), item(20), item(30)]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    geometry(viewport, 500);
    for (let i = 0; i < 8; i++) layoutChange();
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });

  it("does not load from layout changes when the viewport already scrolls", () => {
    const loadOlder = vi.fn();
    render(<TimelineView threadKey="42" items={[makeItem({ item_id: "30.0", kind: "agent_chat", payload: "tail" })]}
      hasMoreOlder onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport, 1200);
    for (let i = 0; i < 10; i++) layoutChange();
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
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport, 1200);
    viewport.scrollTop = 500;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    viewport.scrollTop = 100;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    act(() => useTimelineStore.getState().prependOlder(rows(1, 200), true));
    expect(useTimelineStore.getState().items).toHaveLength(210);
    expect(container.querySelectorAll(".timeline-item").length).toBeLessThan(80);
    expect(container.querySelector("[data-timeline-spacer]")).not.toBeNull();
    viewport.scrollTop = 600;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    expect(useTimelineStore.getState().items).toHaveLength(200);
  });

  it("loads one page per released pull and ignores compensation and momentum after the page lands", () => {
    let rowTop = 0;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        const viewport = this.closest<HTMLElement>('[data-slot="scroll-area-viewport"]');
        const reserve = Number.parseFloat(viewport?.querySelector<HTMLElement>("[data-history-pull-space]")?.style.height ?? "0") || 0;
        if (!this.getAttribute("data-item-id")) return new DOMRect(0, 0, 400, 600);
        return rowRect(rowTop + reserve - (viewport?.scrollTop ?? 0));
      },
    );
    const loadOlder = vi.fn();
    const item = (id: number) => makeItem({ item_id: `${id}.0`, kind: "agent_chat", payload: `row ${id}` });
    const items = [item(100)];
    const { rerender } = render(
      <TimelineView threadKey="42" items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport, 1200);
    viewport.scrollTop = 500;
    act(() => { fireEvent.scroll(viewport); });
    viewport.scrollTop = 0;
    act(() => { fireEvent.scroll(viewport); });
    expect(loadOlder).not.toHaveBeenCalled();
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledOnce();

    rerender(<TimelineView threadKey="42" items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rowTop = 300;
    rerender(<TimelineView threadKey="42" items={[item(90), ...items]} hasMoreOlder onLoadOlder={loadOlder} />);
    expect(viewport.scrollTop).toBe(300);
    act(() => { fireEvent.scroll(viewport); }); // the compensation echo
    for (let i = 0; i < 6; i++) {
      expect(historyWheel(viewport, -100, true).defaultPrevented).toBe(true);
    }
    expect(viewport.scrollTop).toBe(300);
    expect(loadOlder).toHaveBeenCalledOnce();

    // A subsequent arrival remains inert. A fresh released pull can load again.
    rowTop = 0;
    viewport.scrollTop = 0;
    act(() => { fireEvent.scroll(viewport); });
    historyWheel(viewport, -100, true);
    expect(loadOlder).toHaveBeenCalledOnce();
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });
});
