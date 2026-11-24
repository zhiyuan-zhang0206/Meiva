// ScrollArea — viewportClassName plumbing. The timeline relies on this to set
// `overflow-anchor: none` on the inner scroll viewport (the element that
// actually scrolls), so the prop must land on the viewport, not the root.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NonceProvider } from "@/lib/auth/nonce-context";
import { ScrollArea } from "./scroll-area";

const defaultResizeObserver = globalThis.ResizeObserver;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  globalThis.ResizeObserver = defaultResizeObserver;
});

function renderScrollArea() {
  render(
    <ScrollArea>
      <div>content</div>
    </ScrollArea>,
  );
  const root = document.querySelector('[data-slot="scroll-area"]');
  expect(root).toBeInstanceOf(HTMLElement);
  return root as HTMLElement;
}

function getScrollbar() {
  const scrollbar = document.querySelector('[data-slot="scroll-area-scrollbar"]');
  expect(scrollbar).toBeInstanceOf(HTMLElement);
  return scrollbar as HTMLElement;
}

function mockScrollableGeometry() {
  const observations: {
    callback: ResizeObserverCallback;
    observer: ResizeObserver;
    target: Element;
  }[] = [];

  class ControlledResizeObserver implements ResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    disconnect() {
      for (let index = observations.length - 1; index >= 0; index -= 1) {
        if (observations[index].observer === this) observations.splice(index, 1);
      }
    }
    observe(target: Element) {
      observations.push({ callback: this.callback, observer: this, target });
    }
    unobserve(target: Element) {
      const index = observations.findIndex(
        (observation) => observation.observer === this && observation.target === target,
      );
      if (index >= 0) observations.splice(index, 1);
    }
  }

  globalThis.ResizeObserver = ControlledResizeObserver;

  return () => {
    act(() => {
      for (const { callback, observer, target } of observations) {
        callback([{ target } as ResizeObserverEntry], observer);
      }
      vi.advanceTimersByTime(30);
    });
  };
}

// meiva:note
// jsdom has no layout: every element reports 0 for its own geometry, so Radix never
// sees overflow and never mounts a thumb. Give the elements this test rendered the
// numbers a 100px viewport over 400px of content has — on the elements themselves,
// not on a prototype shared with the rest of the process.
function defineScrollableGeometry() {
  const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
  const track = document.querySelector('[data-slot="scroll-area-scrollbar"]');
  expect(viewport).toBeInstanceOf(HTMLElement);
  expect(track).toBeInstanceOf(HTMLElement);
  Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 400 });
  Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(viewport, "offsetHeight", { configurable: true, value: 100 });
  Object.defineProperty(track, "clientHeight", { configurable: true, value: 100 });
}

describe("ScrollArea", () => {
  it("forwards the page nonce to Radix's injected scrollbar style", () => {
    render(
      <NonceProvider value="request-nonce">
        <ScrollArea>
          <div>content</div>
        </ScrollArea>
      </NonceProvider>,
    );

    const style = document.querySelector('[data-slot="scroll-area"]')?.querySelector("style");
    expect(style?.getAttribute("nonce")).toBe("request-nonce");
  });

  it("applies viewportClassName to the inner scroll viewport", () => {
    render(
      <ScrollArea viewportClassName="[overflow-anchor:none]">
        <div data-testid="child">content</div>
      </ScrollArea>,
    );
    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
    expect(viewport).not.toBeNull();
    expect(viewport?.className).toContain("overflow-anchor");
    // The base viewport classes are preserved alongside it.
    expect(viewport?.className).toContain("size-full");
    // And the content still renders.
    expect(screen.getByTestId("child")).toBeTruthy();
  });

  it("root carries overflow-hidden (required by Radix for correct scroll behavior)", () => {
    render(
      <ScrollArea>
        <div>content</div>
      </ScrollArea>,
    );
    const root = document.querySelector('[data-slot="scroll-area"]');
    expect(root).not.toBeNull();
    expect(root?.className).toContain("overflow-hidden");
  });

  it("omitting viewportClassName leaves the viewport with only its base classes", () => {
    render(
      <ScrollArea>
        <div>content</div>
      </ScrollArea>,
    );
    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');
    expect(viewport?.className).toContain("size-full");
    expect(viewport?.className).not.toContain("overflow-anchor");
  });

  it("shows the scrollbar while the root is scrolling", () => {
    const root = renderScrollArea();

    fireEvent.scroll(root);

    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
  });

  it("hides the scrollbar after 800 ms without another scroll", () => {
    vi.useFakeTimers();
    const root = renderScrollArea();
    fireEvent.scroll(root);

    act(() => {
      vi.advanceTimersByTime(800);
    });

    expect(getScrollbar().getAttribute("data-visible")).toBe("false");
  });

  it("re-arms the idle timer when another scroll starts", () => {
    vi.useFakeTimers();
    const root = renderScrollArea();
    fireEvent.scroll(root);
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("false");

    fireEvent.scroll(root);

    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(400);
    });
    fireEvent.scroll(root);
    act(() => {
      vi.advanceTimersByTime(799);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(getScrollbar().getAttribute("data-visible")).toBe("false");
  });

  it("shows an input-driven scrollbar only after reader input and through subsequent scroll events", () => {
    vi.useFakeTimers();
    render(<ScrollArea scrollbarActivity="input" viewportTestId="timeline-viewport">content</ScrollArea>);
    const viewport = screen.getByTestId("timeline-viewport");
    const bar = getScrollbar();

    fireEvent.scroll(viewport);
    expect(bar.dataset.visible).toBe("false");
    const pinch = new WheelEvent("wheel", { bubbles: true, deltaY: 50 });
    Object.defineProperty(pinch, "ctrlKey", { value: true });
    fireEvent(viewport, pinch);
    expect(bar.dataset.visible).toBe("false");
    fireEvent.wheel(viewport, { deltaY: 50 });
    expect(bar.dataset.visible).toBe("true");

    act(() => { vi.advanceTimersByTime(600); });
    fireEvent.scroll(viewport);
    act(() => { vi.advanceTimersByTime(600); });
    expect(bar.dataset.visible).toBe("true");
    act(() => { vi.advanceTimersByTime(200); });
    fireEvent.scroll(viewport);
    expect(bar.dataset.visible).toBe("false");

    fireEvent.keyDown(viewport, { key: "PageDown" });
    expect(bar.dataset.visible).toBe("true");
    act(() => { vi.advanceTimersByTime(800); });
    fireEvent.touchMove(viewport);
    expect(bar.dataset.visible).toBe("true");
  });

  it("keeps the scrollbar visible while its thumb has focus", () => {
    vi.useFakeTimers();
    const flushResizeObservers = mockScrollableGeometry();
    renderScrollArea();
    defineScrollableGeometry();
    flushResizeObservers();
    const scrollbar = getScrollbar();
    const thumb = document.querySelector('[data-slot="scroll-area-thumb"]');
    expect(thumb).toBeInstanceOf(HTMLElement);

    act(() => (thumb as HTMLElement).focus());

    expect(document.activeElement).toBe(thumb);
    expect(thumb?.getAttribute("tabindex")).toBe("0");
    expect(scrollbar.className).toContain("focus-within:opacity-100");
  });

  it("reveals the scrollbar on track hover without revealing it on content hover", () => {
    const root = renderScrollArea();
    const scrollbar = getScrollbar();

    fireEvent.pointerEnter(root);
    expect(scrollbar.getAttribute("data-visible")).toBe("false");
    expect(root.className).not.toContain("hover:opacity-100");
    expect(scrollbar.className).toContain("hover:opacity-100");
    expect(scrollbar.className).not.toContain("pointer-events-none");
    expect(scrollbar.className).toContain("transition-opacity duration-300");
  });

  it("repositions the thumb on EVERY viewport scroll event, mid-gesture", () => {
    // Regression (2026-09-08 user report): the thumb froze for the whole
    // scroll gesture and only jumped at the next discrete event, because the
    // position update depended on Radix's internal rAF polling loop (absent
    // from the deployed bundle). ScrollArea now positions the thumb from the
    // viewport's own scroll events, so a second event inside the same gesture
    // must move the thumb again.
    vi.useFakeTimers();
    const flushResizeObservers = mockScrollableGeometry();
    renderScrollArea();

    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]');

    // Element-level metrics (set BEFORE the resize flush so Radix's own size
    // measurement agrees with ours): content 400 over a 100px viewport on a
    // 100px track. Radix derives a 25px thumb (100 * 100/400); mirror that in
    // the thumb's own offsetHeight below so every writer computes the same
    // travel (100 - 25 = 75) and the same translate.
    defineScrollableGeometry();
    Object.defineProperty(viewport, "scrollTop", { configurable: true, writable: true, value: 0 });
    flushResizeObservers();

    const thumb = document.querySelector('[data-slot="scroll-area-thumb"]');
    expect(thumb).toBeInstanceOf(HTMLElement);
    Object.defineProperty(thumb, "offsetHeight", { configurable: true, value: 25 });

    viewport!.scrollTop = 100;
    fireEvent.scroll(viewport as HTMLElement);
    expect((thumb as HTMLElement).style.transform).toBe("translate3d(0, 25px, 0)");

    // A second scroll event in the same gesture (no idle gap) must move the
    // thumb again — the case that froze without the direct scroll listener.
    viewport!.scrollTop = 200;
    fireEvent.scroll(viewport as HTMLElement);
    expect((thumb as HTMLElement).style.transform).toBe("translate3d(0, 50px, 0)");
  });

  it("keeps the scrollbar visible for a pointer drag until the release idle window", () => {
    vi.useFakeTimers();
    renderScrollArea();
    const scrollbar = getScrollbar();

    fireEvent.pointerDown(scrollbar, { button: 0, pointerId: 1 });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");

    fireEvent.pointerUp(scrollbar, { button: 0, pointerId: 1 });
    expect(scrollbar.getAttribute("data-visible")).toBe("true");
    act(() => {
      vi.advanceTimersByTime(800);
    });
    expect(scrollbar.getAttribute("data-visible")).toBe("false");
  });

  it("stamps viewportTestId on the inner viewport when provided", () => {
    render(
      <ScrollArea viewportTestId="activity-scroll-viewport">
        <div>content</div>
      </ScrollArea>,
    );
    const viewport = screen.getByTestId("activity-scroll-viewport");
    expect(viewport.getAttribute("data-slot")).toBe("scroll-area-viewport");
  });

  it("stamps viewportId on the inner viewport so id lookups reach the real scroller", () => {
    render(
      <ScrollArea viewportId="page-scroll">
        <div>content</div>
      </ScrollArea>,
    );
    const viewport = document.getElementById("page-scroll");
    expect(viewport?.getAttribute("data-slot")).toBe("scroll-area-viewport");
  });

  it("mounts a horizontal scrollbar only when showHorizontalScrollbar is set", () => {
    const { unmount } = render(
      <ScrollArea>
        <div>content</div>
      </ScrollArea>,
    );
    const orientations = () =>
      Array.from(document.querySelectorAll('[data-slot="scroll-area-scrollbar"]')).map((bar) =>
        bar.getAttribute("data-orientation"),
      );
    expect(orientations()).toEqual(["vertical"]);
    unmount();

    render(
      <ScrollArea showHorizontalScrollbar>
        <div>content</div>
      </ScrollArea>,
    );
    expect(orientations().sort()).toEqual(["horizontal", "vertical"]);
  });

  it("observes the content wrapper so late-loading content repositions the thumb", () => {
    const observed: Element[] = [];
    class RecordingResizeObserver implements ResizeObserver {
      disconnect = () => undefined;
      observe(target: Element) {
        observed.push(target);
      }
      unobserve = () => undefined;
    }
    globalThis.ResizeObserver = RecordingResizeObserver;
    renderScrollArea();

    const viewport = document.querySelector('[data-slot="scroll-area-viewport"]')!;
    expect(observed).toContain(viewport);
    expect(observed).toContain(viewport.firstElementChild);
  });

  it("does not flash an outer scrollbar when a nested ScrollArea scrolls", () => {
    render(
      <ScrollArea data-testid="outer-area" viewportTestId="outer-vp">
        <ScrollArea data-testid="inner-area" viewportTestId="inner-vp">
          <div>nested content</div>
        </ScrollArea>
      </ScrollArea>,
    );
    const outerBar = screen
      .getByTestId("outer-area")
      .querySelector(':scope > [data-slot="scroll-area-scrollbar"]')!;
    const innerBar = screen
      .getByTestId("inner-area")
      .querySelector(':scope > [data-slot="scroll-area-scrollbar"]')!;

    fireEvent.scroll(screen.getByTestId("inner-vp"));

    expect(innerBar.getAttribute("data-visible")).toBe("true");
    expect(outerBar.getAttribute("data-visible")).toBe("false");
  });
  it("renders only a horizontal scrollbar for a horizontal-only region", () => {
    render(
      <ScrollArea showVerticalScrollbar={false} showHorizontalScrollbar viewportTestId="h-only">
        <div>content</div>
      </ScrollArea>,
    );
    const orientations = Array.from(document.querySelectorAll('[data-slot="scroll-area-scrollbar"]')).map((bar) =>
      bar.getAttribute("data-orientation"),
    );
    expect(orientations).toEqual(["horizontal"]);
    const viewport = screen.getByTestId("h-only");
    expect(viewport.hasAttribute("data-scroll-y")).toBe(false);
    expect(viewport.style.overflowY).toBe("hidden");
    expect(viewport.style.overflowX).toBe("scroll");
  });

  it("marks a vertical viewport with data-scroll-y", () => {
    render(<ScrollArea viewportTestId="v-area">content</ScrollArea>);
    expect(screen.getByTestId("v-area").getAttribute("data-scroll-y")).toBe("true");
  });

  it("lights the outer scrollbar for vertical input over a nested horizontal-only region", () => {
    vi.useFakeTimers();
    render(
      <ScrollArea data-testid="outer-area" viewportTestId="outer-vp" scrollbarActivity="input">
        <ScrollArea showVerticalScrollbar={false} showHorizontalScrollbar viewportTestId="inner-vp">
          <div data-testid="inner-content">wide</div>
        </ScrollArea>
      </ScrollArea>,
    );
    const outerBar = screen
      .getByTestId("outer-area")
      .querySelector<HTMLElement>(':scope > [data-slot="scroll-area-scrollbar"]')!;
    const content = screen.getByTestId("inner-content");

    fireEvent.wheel(content, { deltaY: 40 });
    expect(outerBar.dataset.visible).toBe("true");
    act(() => { vi.advanceTimersByTime(800); });
    expect(outerBar.dataset.visible).toBe("false");

    fireEvent.touchMove(content);
    expect(outerBar.dataset.visible).toBe("true");
    act(() => { vi.advanceTimersByTime(800); });

    fireEvent.keyDown(content, { key: "PageDown" });
    expect(outerBar.dataset.visible).toBe("true");
  });

  it("observes the wrapper's inner child of a horizontal region and follows a replaced child", async () => {
    const observed = new Set<Element>();
    class TrackingResizeObserver implements ResizeObserver {
      disconnect = () => observed.clear();
      observe(target: Element) {
        observed.add(target);
      }
      unobserve(target: Element) {
        observed.delete(target);
      }
    }
    globalThis.ResizeObserver = TrackingResizeObserver;
    const { rerender } = render(
      <ScrollArea showVerticalScrollbar={false} showHorizontalScrollbar viewportTestId="h-vp">
        <pre data-testid="first">a</pre>
      </ScrollArea>,
    );
    const viewport = screen.getByTestId("h-vp");
    expect(observed.has(viewport)).toBe(true);
    expect(observed.has(screen.getByTestId("first"))).toBe(true);

    rerender(
      <ScrollArea showVerticalScrollbar={false} showHorizontalScrollbar viewportTestId="h-vp">
        <table data-testid="second" />
      </ScrollArea>,
    );
    await waitFor(() => expect(observed.has(screen.getByTestId("second"))).toBe(true));
    expect(observed.has(viewport)).toBe(true);
  });
});
