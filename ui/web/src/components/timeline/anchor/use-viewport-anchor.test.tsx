import { act, render } from "@testing-library/react";
import { useLayoutEffect, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { POINTER_STICKY_THRESHOLDS, TOUCH_STICKY_THRESHOLDS, type StickyThresholds } from "@/lib/layout/sticky";

import { timelineLayout } from "../view/timeline-test-mock";
import { TAIL, type ViewAnchor } from "./dom";
import { useViewportAnchor, type AnchorChangeCause, type ViewportAnchor } from "./use-viewport-anchor";

// meiva:note
// The writer alone, over a bare log: rows are 100px (12px apart, the log padded 52/24), the
// viewport 600px and the mouse bottom zone 30px. Twenty rows put row n at 52 + (n-1)*112 in scroll
// space and the natural end at 1704.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

const ids = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => String(from + i));
const rowAt = (itemId: string, offset: number): ViewAnchor => ({ kind: "row", row: { itemId, rank: 0 }, offset });

function harness(
  initial: string[],
  heights: Record<string, number> = {},
  options: { thresholds?: StickyThresholds; viewportHeight?: number } = {},
) {
  const thresholds = options.thresholds ?? POINTER_STICKY_THRESHOLDS;
  const layout = timelineLayout({
    viewportHeight: options.viewportHeight,
    rowHeight: (row) => heights[row.dataset.itemId ?? ""] ?? 100,
  });
  const changes: { anchor: ViewAnchor; cause: AnchorChangeCause }[] = [];
  const onChange = (anchor: ViewAnchor, cause: AnchorChangeCause) => changes.push({ anchor, cause });
  let controls: ViewportAnchor | null = null;
  function Harness({ rows }: { rows: string[] }) {
    const wrapperRef = useRef<HTMLDivElement>(null);
    const viewportRef = useRef<HTMLDivElement>(null);
    const contentRef = useRef<HTMLDivElement>(null);
    const reserveRef = useRef<HTMLDivElement>(null);
    const anchor = useViewportAnchor({
      wrapperRef, viewportRef, contentRef, reserveRef, thresholds,
      onChange, onUnresolved: () => undefined,
    }).controls;
    controls = anchor;
    // meiva:note
    // The view applies the anchor after every commit, as TimelineView does once placement has run.
    useLayoutEffect(() => { anchor.apply(); });
    return (
      <div ref={wrapperRef}>
        <div ref={viewportRef} data-testid="scroll-viewport" data-slot="scroll-area-viewport">
          <div data-timeline-column>
            <div ref={contentRef} role="log" className="space-y-3">
              {rows.map((id) => <div key={id} className="timeline-item" data-item-id={id} data-display-rank="0" />)}
            </div>
            <div ref={reserveRef} data-timeline-reserve />
          </div>
        </div>
      </div>
    );
  }
  const view = render(<Harness rows={initial} />);
  return {
    ...layout,
    changes,
    rows: (rows: string[]) => view.rerender(<Harness rows={rows} />),
    place: (anchor: ViewAnchor) => act(() => controls!.place(anchor)),
    placeHeld: (anchor: ViewAnchor) => act(() => controls!.place(anchor, { hold: true })),
    current: () => controls!.current(),
    heights,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the viewport anchor writer", () => {
  it("pins the tail to the natural end as content grows, from rows or from heights", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    expect(view.top()).toBe(1704);

    view.rows(ids(1, 22));
    expect(view.top()).toBe(1704 + 2 * 112);
    expect(view.top()).toBe(view.naturalEnd());

    view.heights["21"] = 400;
    view.resize();
    expect(view.top()).toBe(1704 + 2 * 112 + 300);
    expect(view.top()).toBe(view.naturalEnd());
  });

  it("keeps the anchor row at its offset when content above it grows", () => {
    const view = harness(ids(3, 20));
    view.place(rowAt("10", 100));
    expect(view.rowTop("10")).toBe(100);

    view.rows(ids(1, 20));
    expect(view.rowTop("10")).toBe(100);

    view.heights["4"] = 350;
    view.resize();
    expect(view.rowTop("10")).toBe(100);

    view.rows(ids(1, 25));
    expect(view.rowTop("10")).toBe(100);
  });

  it("reads a scroll it did not write back from the DOM as the reader's new place", () => {
    const view = harness(ids(1, 20));
    view.place(rowAt("10", 100));
    view.scrollTo(500);
    // meiva:note
    // Row 5 is the first whose bottom clears the 44px header bar: its top sits at 52 + 448 - 500.
    expect(view.changes.at(-1)).toEqual({ anchor: rowAt("5", 0), cause: "reader" });

    view.heights["7"] = 300;
    view.resize();
    expect(view.rowTop("5")).toBe(0);
    expect(view.rowTop("10")).toBe(560 + 200);
  });

  it("ignores the scroll event that echoes its own write", () => {
    const view = harness(ids(1, 20));
    // meiva:note
    // Row 16 at 38 puts the viewport 10px above the natural end: inside the bottom zone, where a
    // reader's own scroll would read back as the tail.
    view.place(rowAt("16", 38));
    expect(view.top()).toBe(1694);
    view.echo();
    expect(view.changes.filter(({ cause }) => cause === "reader")).toEqual([]);

    view.rows(ids(1, 24));
    expect(view.top()).toBe(1694);
    expect(view.rowTop("16")).toBe(38);
  });

  it("keeps a reader's movement that has no scroll event yet, and still compensates rows above", () => {
    const view = harness(ids(3, 20));
    view.place(rowAt("10", 100));
    const placed = view.top();

    view.drift(placed - 160);
    view.rows(ids(3, 21));
    expect(view.top()).toBe(placed - 160);
    expect(view.rowTop("10")).toBe(260);

    view.rows(ids(1, 21));
    expect(view.top()).toBe(placed - 160 + 2 * 112);
    expect(view.rowTop("10")).toBe(260);

    view.drift(view.top() + 50);
    view.rows(ids(0, 21));
    expect(view.rowTop("10")).toBe(210);
  });

  it("leaves the tail on an upward wheel notch before its scroll event arrives", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    act(() => { view.viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: 8, bubbles: true })); });
    view.rows(ids(1, 21));
    expect(view.top()).toBe(view.naturalEnd());

    act(() => { view.viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: -8, bubbles: true })); });
    expect(view.changes.at(-1)?.cause).toBe("reader");
    expect(view.current()?.kind).toBe("row");
    const left = view.top();
    view.rows(ids(1, 23));
    expect(view.top()).toBe(left);
  });

  it("follows again once the reader scrolls back into the bottom zone", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    view.scrollTo(1500);
    view.rows(ids(1, 21));
    expect(view.top()).toBe(1500);

    view.scrollTo(view.naturalEnd() - 20);
    expect(view.current()).toEqual(TAIL);
    view.rows(ids(1, 23));
    expect(view.top()).toBe(view.naturalEnd());
  });

  it("keeps a follower following on sub-pixel resting jitter, and leaves on any upward scroll away from the end", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    act(() => { view.viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: -1, bubbles: true })); });
    view.scrollTo(view.naturalEnd() - 1);
    expect(view.current()).toEqual(TAIL);
    view.rows(ids(1, 21));
    expect(view.top()).toBe(view.naturalEnd());

    const away = view.naturalEnd() - 5;
    view.scrollTo(away);
    expect(view.current()?.kind).toBe("row");
    view.rows(ids(1, 22));
    expect(view.top()).toBe(away);
  });

  it.each([3, 5, 10, 20])("mobile touch scroll-up of %ipx inside the wide touch bottom zone never snaps back to tail", (delta) => {
    const view = harness(ids(1, 20), {}, { thresholds: TOUCH_STICKY_THRESHOLDS, viewportHeight: 770 });
    view.place(TAIL);
    const tail = view.naturalEnd();
    expect(view.top()).toBe(tail);

    view.touchStart(500);
    view.touchMove(500 + delta);
    expect(view.current()?.kind).toBe("row");
    view.scrollTo(tail - delta);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");
    expect(view.top()).toBe(tail - delta);

    view.writes.length = 0;
    view.rows(ids(1, 22));
    view.heights["22"] = 300;
    view.resize();
    expect(view.current()?.kind).toBe("row");
    expect(view.top()).toBe(tail - delta);
    expect(view.writes).toEqual([]);
  });

  it("releases tail on touchmove upward drag before scroll arrives, ignoring horizontal swipes and 1px taps", () => {
    const view = harness(ids(1, 20), {}, { thresholds: TOUCH_STICKY_THRESHOLDS, viewportHeight: 770 });
    view.place(TAIL);

    view.touchStart(500, 100);
    view.touchMove(501, 100);
    view.touchEnd();
    expect(view.current()).toEqual(TAIL);

    view.touchStart(500, 100);
    view.touchMove(504, 140);
    view.touchEnd();
    expect(view.current()).toEqual(TAIL);

    view.touchStart(500, 100);
    view.touchMove(503, 100);
    expect(view.current()?.kind).toBe("row");
    const left = view.top();
    view.rows(ids(1, 22));
    expect(view.top()).toBe(left);
    view.scrollTo(left - 6);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");
    expect(view.top()).toBe(left - 6);
  });

  it("does not re-stick to tail during sub-pixel upward deceleration or a tiny thumb-lift roll on touch", () => {
    const view = harness(ids(1, 20), {}, { thresholds: TOUCH_STICKY_THRESHOLDS, viewportHeight: 770 });
    view.place(TAIL);
    const tail = view.naturalEnd();

    view.touchStart(500);
    view.touchMove(510);
    for (let step = 1; step <= 18; step += 1) {
      view.scrollTo(tail - step * 0.4);
    }
    const liftedAt = tail - 18 * 0.4 + 2;
    view.scrollTo(liftedAt);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");

    view.scrollTo(liftedAt - 2);
    view.scrollTo(liftedAt - 2.4);
    view.scrollTo(liftedAt - 2.7);
    expect(view.current()?.kind).toBe("row");
    const settled = view.top();
    view.rows(ids(1, 22));
    view.resize();
    expect(view.top()).toBe(settled);
  });

  it("re-sticks to tail on mobile when scrolling back down into the touch bottom zone or reaching the end", () => {
    const view = harness(ids(1, 20), {}, { thresholds: TOUCH_STICKY_THRESHOLDS, viewportHeight: 770 });
    view.place(TAIL);
    const tail = view.naturalEnd();

    view.touchStart(500);
    view.touchMove(510);
    view.scrollTo(tail - 10);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");

    view.scrollTo(tail - 300);
    view.touchStart(300);
    view.touchMove(120);
    view.scrollTo(tail - 100);
    view.touchEnd();
    expect(view.current()).toEqual(TAIL);
    view.rows(ids(1, 22));
    expect(view.top()).toBe(view.naturalEnd());
  });

  it("keeps upward touch direction when older rows prepend mid-drag inside the touch bottom zone", () => {
    const view = harness(ids(10, 30), {}, { thresholds: TOUCH_STICKY_THRESHOLDS, viewportHeight: 770 });
    view.place(TAIL);
    const tail = view.naturalEnd();

    view.touchStart(500);
    view.touchMove(512);
    view.scrollTo(tail - 12);
    const row = view.rowTop("24");
    view.rows([...ids(1, 9), ...ids(10, 30)]);
    expect(view.rowTop("24")).toBe(row);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");
    expect(view.rowTop("24")).toBe(row);
  });

  it("keeps the row under a held finger in place when older rows land above it", () => {
    const view = harness(ids(10, 30));
    view.place(TAIL);
    view.scrollTo(1000);
    view.touchStart();
    view.scrollTo(900);
    const row = view.rowTop("15");
    view.rows([...ids(1, 9), ...ids(10, 30)]);
    expect(view.rowTop("15")).toBe(row);
    view.touchEnd();
    expect(view.rowTop("15")).toBe(row);
  });

  it("does nothing while a touch is held, and reads the place where the finger lifts", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    view.touchStart();
    view.rows(ids(1, 21));
    expect(view.top()).toBe(1704);
    view.scrollTo(1500);
    view.rows(ids(1, 22));
    expect(view.top()).toBe(1500);
    view.touchEnd();
    expect(view.current()?.kind).toBe("row");
    view.rows(ids(1, 23));
    expect(view.top()).toBe(1500);
  });

  it("adds reserve space below the content only while a header toggle holds it", () => {
    const view = harness(ids(1, 20));
    view.place(rowAt("20", 56));
    expect(view.reserve()).toBe(0);
    expect(view.top()).toBe(1704);

    view.placeHeld(rowAt("20", 56));
    // meiva:note
    // Row 20 sits at 2180 in a 2304px log: 2180 - 56 + 600 - 2304 of space below the content.
    expect(view.reserve()).toBe(420);
    expect(view.top()).toBe(2124);
    expect(view.rowTop("20")).toBe(56);
  });

  it("releases the reserve when the reader scrolls upward to or past the natural end", () => {
    const view = harness(ids(1, 20));
    view.placeHeld(rowAt("20", 56));
    expect(view.reserve()).toBe(420);

    // meiva:note
    // Inside the reserve the reader's own row keeps what it needs: row 18 read at 56 needs 196.
    view.scrollTo(1900);
    expect(view.top()).toBe(1900);
    expect(view.rowTop("18")).toBe(56);
    expect(view.reserve()).toBe(196);
    view.resize();
    expect(view.top()).toBe(1900);
    expect(view.reserve()).toBe(196);
    view.scrollTo(1705);
    expect(view.reserve()).toBeGreaterThan(0);

    view.scrollTo(1704);
    expect(view.reserve()).toBe(0);
    expect(view.top()).toBe(1704);
    view.resize();
    expect(view.reserve()).toBe(0);
  });
});
