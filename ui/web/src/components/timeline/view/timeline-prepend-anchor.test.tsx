import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, historyWheel, makeItem, pullOlderAtTop, render } from "./timeline-test-harness";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);
describe("load-older prepend anchor (#659, #817, #1272; release-to-load)", () => {
  const rect = (top: number): DOMRect =>
    ({ top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) });
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });
  const triggerAtTop = (viewport: HTMLElement) => {
    Object.defineProperty(viewport, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    viewport.scrollTop = 500;
    viewport.dispatchEvent(new Event("scroll"));
    viewport.scrollTop = 0;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    pullOlderAtTop(viewport);
  };
  it("momentum arrival alone does not load, and a subsequent released pull loads exactly once", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
    ];
    render(<TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    Object.defineProperty(viewport, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    viewport.scrollTop = 50;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).not.toHaveBeenCalled();
    viewport.scrollTop = 0;
    viewport.dispatchEvent(new Event("scroll"));
    historyWheel(viewport, -200, true);
    expect(loadOlder).not.toHaveBeenCalled();
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    viewport.scrollTop = 0;
    viewport.dispatchEvent(new Event("scroll"));
    expect(loadOlder).toHaveBeenCalledTimes(1);
  });
  it("anchors to the first REAL content node, not the system prompt (0.0) — zero height jitter (#817)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(
      <TimelineView
        items={[...items, makeItem({ item_id: "12.0", kind: "agent_chat", payload: "twelve" })]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(0);
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(1),
          makeItem({ item_id: "12.0", kind: "agent_chat", payload: "twelve" }),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(250);
  });
  it("skips re-attached standing head notes — anchor and front signal land on the first real item", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "1.0", kind: "system_marker", source: "exec_timeout", payload: "t" }),
      makeItem({ item_id: "2.0", kind: "system_marker", source: "memory", payload: "m" }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          ...items.slice(1, 3),
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(3),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(250);
  });
  it("uses four-part historical ids for the prepend landing signal and exact anchor lookup", () => {
    const loadOlder = vi.fn();
    const recentHistoricalId = "s1.newer-boundary.1.0";
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: recentHistoricalId, kind: "agent_chat", payload: "recent historical" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "one" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === recentHistoricalId) return rect(moved ? 320 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    moved = true;
    const olderHistoricalId = "s2.older-boundary.1.0";
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: olderHistoricalId, kind: "agent_chat", payload: "older historical" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(320);
  });
  it("skips pinned compact-summary context when its turn is collapsed", () => {
    const loadOlder = vi.fn();
    const summaryId = "s1.boundary.0.0";
    const firstRealId = "s1.boundary.1.0";
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: summaryId, kind: "inbound_compact_summary", payload: "sum" }),
      makeItem({ item_id: firstRealId, kind: "agent_chat", payload: "real" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === firstRealId) return rect(moved ? 200 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "s2.older.1.0", kind: "agent_chat", payload: "older" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(200);
  });
  it("skips ephemeral _marker rows too — the anchor is the first real message (#817)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "_marker.compact_done", kind: "system_marker", source: "lifecycle", payload: "compact_done" }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 250 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(250);
  });
  it("still compensates when the thread has no system prompt (front item is real)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(moved ? 180 : 0);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    moved = true;
    rerender(
      <TimelineView
        items={[
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items,
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(180);
  });
  it("abandons the anchor when the content above did not grow (delta <= 0 — compact/reset, not a landing)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
    ];
    let top = 100;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "10.0") return rect(top);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    top = 80;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(0);
  });
  it("anchors to the 0.0 prompt node when no real item is visible — landing does not scroll (#1272)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
    ];
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.dataset.itemId === "0.0") return rect(0);
        if (this.dataset.itemId === "10.0") return rect(2000);
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(0);
  });
  it("materializes the full prepend and keeps its real height after compensation", () => {
    const loadOlder = vi.fn();
    const tallRect = (top: number, height: number): DOMRect =>
      ({ top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: 0, toJSON: () => ({}) });
    const EST = 80;
    const PROMPT = 100;
    const CONTENT_TOP = -340;
    const realHeights: Record<string, number> = { "5.0": 500, "6.0": 400, "7.0": 90, "8.0": 70 };
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        const vp = document.querySelector<HTMLElement>('[data-testid="scroll-viewport"]');
        if (this === vp) {
          return { top: 0, bottom: 752, left: 0, right: 0, width: 0, height: 752, x: 0, y: 0, toJSON: () => ({}) };
        }
        if (!vp) return rect(0);
        const rows = [...vp.querySelectorAll<HTMLElement>(".timeline-item")];
        const idx = rows.indexOf(this);
        if (idx < 0) return rect(0);
        let top = CONTENT_TOP;
        for (let j = 0; j < idx; j++) {
          const row = rows[j];
          const id = row.dataset.itemId ?? "";
          const forced = row.style.contentVisibility === "visible";
          top += id === "0.0" ? PROMPT : forced ? (realHeights[id] ?? EST) : EST;
        }
        const id = this.dataset.itemId ?? "";
        const height =
          id === "0.0" ? PROMPT : this.style.contentVisibility === "visible" ? (realHeights[id] ?? EST) : EST;
        return tallRect(top - vp.scrollTop, height);
      },
    );
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "7.0", kind: "agent_chat", payload: "seven" }),
      makeItem({ item_id: "8.0", kind: "agent_chat", payload: "eight" }),
      makeItem({ item_id: "9.0", kind: "agent_chat", payload: "nine" }),
    ];
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    const readingTop = viewport.querySelector<HTMLElement>('[data-item-id="9.0"]')!.getBoundingClientRect().top;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          makeItem({ item_id: "6.0", kind: "agent_chat", payload: "six" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(900);
    expect(viewport.querySelector<HTMLElement>('[data-item-id="9.0"]')!.getBoundingClientRect().top).toBe(readingTop);
  });
  it("preserves the reading position when the user scrolled during the fetch (document-space delta, not the trigger-time viewport top)", () => {
    const loadOlder = vi.fn();
    const items = [
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null }),
      makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" }),
      makeItem({ item_id: "11.0", kind: "agent_chat", payload: "eleven" }),
    ];
    let moved = false;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        if (this.getAttribute("data-testid") === "scroll-viewport") {
          return { top: 0, bottom: 752, left: 0, right: 0, width: 0, height: 752, x: 0, y: 0, toJSON: () => ({}) };
        }
        const vp = document.querySelector<HTMLElement>('[data-testid="scroll-viewport"]');
        const st = vp?.scrollTop ?? 0;
        if (this.dataset.itemId === "10.0") return rect(200 - st + (moved ? 250 : 0));
        return rect(0);
      },
    );
    const { rerender } = render(
      <TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />,
    );
    const viewport = screen.getByTestId("scroll-viewport");
    triggerAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(
      <TimelineView items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />,
    );
    viewport.scrollTop = 0;
    viewport.dispatchEvent(new Event("scroll"));
    moved = true;
    rerender(
      <TimelineView
        items={[
          items[0],
          makeItem({ item_id: "5.0", kind: "agent_chat", payload: "five" }),
          ...items.slice(1),
        ]}
        hasMoreOlder
        loadingOlder
        onLoadOlder={loadOlder}
      />,
    );
    expect(viewport.scrollTop).toBe(250);
  });
});
