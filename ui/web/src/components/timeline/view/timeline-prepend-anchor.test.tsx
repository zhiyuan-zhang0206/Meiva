import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, historyWheel, makeItem, pullOlderAtTop, render, timelineLayout } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);

// meiva:note
// An older page lands above the reader: the row they were reading keeps its place on screen and
// the new rows grow upward, out of view. The layout model gives every row its height (80 unless
// set), 12px apart below the 52px bar-clear padding.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
describe("load-older prepend keeps the reader's row (#659, #817, #1272)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  const chat = (id: string, payload = id) => makeItem({ item_id: id, kind: "agent_chat", payload });
  const prompt = makeItem({ item_id: "0.0", kind: "system_prompt", payload: "prompt", created_at: null });

  function paging(items: BackendTimelineItem[], layout: ReturnType<typeof timelineLayout>) {
    const loadOlder = vi.fn();
    const view = (rows: BackendTimelineItem[], loadingOlder = false) => (
      <TimelineView items={rows} hasMoreOlder loadingOlder={loadingOlder} onLoadOlder={loadOlder} />
    );
    const { rerender } = render(view(items));
    return {
      loadOlder,
      pullAtTop: () => {
        layout.scrollTo(200);
        layout.scrollTo(0);
        pullOlderAtTop(layout.viewport());
        expect(loadOlder).toHaveBeenCalledTimes(1);
        rerender(view(items, true));
      },
      land: (rows: BackendTimelineItem[]) => rerender(view(rows)),
    };
  }

  it("momentum arrival alone does not load, and a fresh upward gesture loads exactly once", () => {
    const layout = timelineLayout({ rowHeight: 800 });
    const loadOlder = vi.fn();
    render(<TimelineView items={[prompt, chat("10.0", "ten")]} hasMoreOlder onLoadOlder={loadOlder} />);
    layout.scrollTo(50);
    expect(loadOlder).not.toHaveBeenCalled();
    layout.scrollTo(0);
    historyWheel(layout.viewport(), -200, true);
    expect(loadOlder).not.toHaveBeenCalled();
    vi.advanceTimersByTime(150);
    pullOlderAtTop(layout.viewport());
    expect(loadOlder).toHaveBeenCalledTimes(1);
    layout.scrollTo(0);
    expect(loadOlder).toHaveBeenCalledTimes(1);
  });

  it("keeps the first visible row in place and lands the older rows above the viewport", () => {
    const layout = timelineLayout({ rowHeight: 300 });
    const items = [chat("10.0"), chat("11.0"), chat("12.0")];
    const history = paging(items, layout);
    history.pullAtTop();
    const readingTop = layout.rowTop("10.0");
    history.land([chat("5.0"), chat("6.0"), ...items]);
    expect(layout.rowTop("10.0")).toBe(readingTop);
    expect(layout.top()).toBe(2 * 312);
    expect(layout.rowTop("6.0")).toBeLessThan(0);
  });

  it("uses four-part historical ids for the landing and the exact row lookup", () => {
    const layout = timelineLayout({ rowHeight: 320 });
    const recent = "s1.newer-boundary.1.0";
    const items = [chat(recent, "recent historical"), chat("1.0", "one"), chat("2.0", "two")];
    const history = paging(items, layout);
    history.pullAtTop();
    const readingTop = layout.rowTop(recent);
    history.land([chat("s2.older-boundary.1.0", "older historical"), ...items]);
    expect(layout.rowTop(recent)).toBe(readingTop);
    expect(layout.top()).toBe(332);
  });

  it("never anchors on an ephemeral _marker row: the real row below it keeps its place", () => {
    const layout = timelineLayout({ rowHeight: 300 });
    const marker = makeItem({ item_id: "_marker.compact_done", kind: "system_marker", source: "lifecycle", payload: "compact_done" });
    const items = [marker, chat("10.0", "ten"), chat("11.0", "eleven")];
    const history = paging(items, layout);
    history.pullAtTop();
    const readingTop = layout.rowTop("10.0");
    // meiva:note
    // The marker is gone by the time the page lands; the reader's row is not.
    history.land([chat("5.0", "five"), chat("10.0", "ten"), chat("11.0", "eleven")]);
    expect(layout.viewport().querySelector("[data-timeline-column]")!.getAttribute("style")).toBeNull();
    expect(layout.rowTop("10.0")).toBe(readingTop);
  });

  it("does not scroll when only the pinned prompt is in view as the page lands (#1272)", () => {
    const layout = timelineLayout({
      rowHeight: 300,
      height: (element) => (element.dataset.turnId === "0.0" ? 700 : undefined),
    });
    const items = [prompt, chat("10.0", "ten")];
    const history = paging(items, layout);
    history.pullAtTop();
    expect(layout.rowTop("10.0")).toBeGreaterThan(600);
    history.land([prompt, chat("5.0", "five"), ...items.slice(1)]);
    expect(layout.top()).toBe(0);
  });

  // meiva:note
  // The system prompt, its standing head notes and re-attached compact summaries stay pinned at the
  // front of every loaded window, so older pages land below them. The reader's anchor skips these
  // heads, so a page landing at the top keeps the row being read in place.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  it.each([
    ["the system prompt", [prompt]],
    ["standing head notes", [
      prompt,
      makeItem({ item_id: "1.0", kind: "system_marker", source: "exec_timeout", payload: "t" }),
      makeItem({ item_id: "2.0", kind: "system_marker", source: "memory", payload: "m" }),
    ]],
    ["a re-attached compact summary", [prompt, makeItem({ item_id: "s1.boundary.0.0", kind: "inbound_compact_summary", payload: "sum" })]],
  ])("keeps the first real row in place when an older page lands under %s (#817)", (_label, head) => {
    const layout = timelineLayout({ rowHeight: 300 });
    const items = [...head, chat("10.0", "ten"), chat("11.0", "eleven")];
    const history = paging(items, layout);
    history.pullAtTop();
    const readingTop = layout.rowTop("10.0");
    history.land([...head, chat("5.0", "five"), ...items.slice(head.length)]);
    expect(layout.rowTop("10.0")).toBe(readingTop);
  });

  it("keeps the row the reader moved to while the page was in flight", () => {
    const layout = timelineLayout({ rowHeight: 300 });
    const items = [chat("10.0"), chat("11.0"), chat("12.0"), chat("13.0")];
    const history = paging(items, layout);
    history.pullAtTop();
    layout.scrollTo(400);
    const readingTop = layout.rowTop("12.0");
    history.land([chat("5.0"), ...items]);
    expect(layout.rowTop("12.0")).toBe(readingTop);
    expect(layout.top()).toBe(400 + 312);
  });

  it("keeps the reading row through a prepend of rows with real, uneven heights", () => {
    const heights: Record<string, number> = { "5.0": 500, "6.0": 400, "7.0": 90, "8.0": 70, "9.0": 300 };
    const layout = timelineLayout({ rowHeight: (row) => heights[row.dataset.itemId ?? ""] ?? 80 });
    const items = [chat("7.0"), chat("8.0"), chat("9.0"), chat("10.0", "ten"), makeItem({ item_id: "11.0", kind: "agent_chat", payload: "x" })];
    heights["10.0"] = 400;
    heights["11.0"] = 400;
    const history = paging(items, layout);
    history.pullAtTop();
    layout.scrollTo(200);
    const reading = layout.rowTop("9.0");
    history.land([chat("5.0"), chat("6.0"), ...items]);
    expect(layout.rowTop("9.0")).toBe(reading);
    expect(layout.top()).toBe(200 + 500 + 12 + 400 + 12);
  });
});
