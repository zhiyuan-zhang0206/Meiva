import { act, render } from "@testing-library/react";
import { useLayoutEffect, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { POINTER_STICKY_THRESHOLDS } from "@/lib/layout/sticky";

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

function harness(initial: string[], heights: Record<string, number> = {}) {
  const layout = timelineLayout({ rowHeight: (row) => heights[row.dataset.itemId ?? ""] ?? 100 });
  const changes: { anchor: ViewAnchor; cause: AnchorChangeCause }[] = [];
  const onChange = (anchor: ViewAnchor, cause: AnchorChangeCause) => changes.push({ anchor, cause });
  let controls: ViewportAnchor | null = null;
  function Harness({ rows }: { rows: string[] }) {
    const wrapperRef = useRef<HTMLDivElement>(null);
    const viewportRef = useRef<HTMLDivElement>(null);
    const contentRef = useRef<HTMLDivElement>(null);
    const reserveRef = useRef<HTMLDivElement>(null);
    const anchor = useViewportAnchor({
      wrapperRef, viewportRef, contentRef, reserveRef, thresholds: POINTER_STICKY_THRESHOLDS,
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

  it("keeps a follower following through a sub-zone twitch or nudge", () => {
    const view = harness(ids(1, 20));
    view.place(TAIL);
    act(() => { view.viewport().dispatchEvent(new WheelEvent("wheel", { deltaY: -1, bubbles: true })); });
    view.scrollTo(view.naturalEnd() - 2);
    expect(view.current()).toEqual(TAIL);
    view.scrollTo(view.naturalEnd() - 20);
    expect(view.current()).toEqual(TAIL);
    view.rows(ids(1, 22));
    expect(view.top()).toBe(view.naturalEnd());
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
