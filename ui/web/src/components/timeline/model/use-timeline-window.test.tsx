import { act, render } from "@testing-library/react";
import { useLayoutEffect, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTimelineWindow, type WindowGroup } from "./use-timeline-window";

const VIEWPORT_PX = 800;
const GROUP_COUNT = 60;
const REAL_HEIGHT_PX = 600;
const ESTIMATE_PX = 80;
const GAP_PX = 12;
const ANCHOR_INDEX = 30;
const ANCHOR_OFFSET_PX = -300;

const GROUPS: WindowGroup[] = Array.from({ length: GROUP_COUNT }, (_, index) => ({
  key: `g${index}`,
  rank: 0,
  itemIds: [`${index}.0`],
  estimatedHeight: ESTIMATE_PX,
  expandedRows: null,
}));

function Harness({ onWrite }: { onWrite: () => void }) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const anchoredRef = useRef<{ offset: number } | null>(null);
  const { range } = useTimelineWindow({
    groups: GROUPS,
    enabled: true,
    turnRows: 100,
    measureRows: 10,
    identity: "thread",
    viewportRef,
    contentRef,
    isFollowing: () => false,
    pin: { itemId: `${ANCHOR_INDEX}.0`, rank: 0 },
    tail: false,
  });
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const anchor = viewport?.querySelector<HTMLElement>(`[data-virtual-group="g${ANCHOR_INDEX}"]`);
    if (!viewport || !anchor) return;
    anchoredRef.current ??= { offset: ANCHOR_OFFSET_PX };
    const target = Math.max(0, viewport.scrollTop + anchor.getBoundingClientRect().top - anchoredRef.current.offset);
    if (Math.abs(viewport.scrollTop - target) < 0.5) return;
    viewport.scrollTop = target;
    onWrite();
  });
  return (
    <div ref={viewportRef} data-testid="viewport">
      <div ref={contentRef}>
        {range.before > 0 ? <div data-spacer style={{ height: range.before }} /> : null}
        {GROUPS.slice(range.start, range.end).map((group) => (
          <div key={group.key} data-virtual-group={group.key} />
        ))}
        {range.after > 0 ? <div data-spacer style={{ height: range.after }} /> : null}
      </div>
    </div>
  );
}

function installLayout(viewport: HTMLElement) {
  let scrollTop = 0;
  Object.defineProperty(viewport, "scrollTop", {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => { scrollTop = value; },
  });
  Object.defineProperty(viewport, "clientHeight", { configurable: true, value: VIEWPORT_PX });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this === viewport) return { top: 0, bottom: VIEWPORT_PX, height: VIEWPORT_PX } as DOMRect;
    const content = viewport.firstElementChild as HTMLElement;
    const children = Array.from(content.children) as HTMLElement[];
    const heightOf = (child: HTMLElement) => child.dataset.spacer === undefined
      ? REAL_HEIGHT_PX
      : Number.parseFloat(child.style.height);
    const index = children.indexOf(this);
    if (index < 0) return { top: 0, bottom: 0, height: 0 } as DOMRect;
    let top = -scrollTop;
    for (let before = 0; before < index; before++) top += heightOf(children[before]) + GAP_PX;
    return { top, bottom: top + heightOf(this), height: heightOf(this) } as DOMRect;
  });
}

describe("useTimelineWindow", () => {
  afterEach(() => vi.restoreAllMocks());

  it("settles when the reader's row sits below unmeasured rows far taller than their estimate", () => {
    vi.stubGlobal("ResizeObserver", class {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    });
    const onWrite = vi.fn();
    const { getByTestId } = render(<Harness onWrite={onWrite} />);
    installLayout(getByTestId("viewport"));
    expect(() => act(() => {
      getByTestId("viewport").scrollTop = 52 + (ANCHOR_INDEX - 3) * (ESTIMATE_PX + GAP_PX);
      getByTestId("viewport").dispatchEvent(new Event("scroll"));
    })).not.toThrow();
    const writes = onWrite.mock.calls.length;
    for (let pass = 0; pass < 4; pass++) act(() => { getByTestId("viewport").dispatchEvent(new Event("scroll")); });
    expect(onWrite.mock.calls.length).toBe(writes);
    vi.unstubAllGlobals();
  });
});
