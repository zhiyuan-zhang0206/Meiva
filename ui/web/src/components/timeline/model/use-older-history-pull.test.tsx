import { act, cleanup } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { useEffect, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useOlderHistoryPull } from "./use-older-history-pull";

function Harness({ thread = "first", available = true, loadPage }: {
  thread?: string; available?: boolean; loadPage: () => boolean;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const { onWheel, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel } =
    useOlderHistoryPull(thread, () => available, loadPage, () => undefined);
  useEffect(() => {
    const viewport = viewportRef.current!;
    const wheel = (event: WheelEvent) => onWheel(event, viewport);
    const start = (event: TouchEvent) => onTouchStart(event, viewport);
    viewport.addEventListener("wheel", wheel, { passive: false });
    viewport.addEventListener("touchstart", start);
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", onTouchEnd);
    viewport.addEventListener("touchcancel", onTouchCancel);
    return () => {
      viewport.removeEventListener("wheel", wheel);
      viewport.removeEventListener("touchstart", start);
      viewport.removeEventListener("touchmove", onTouchMove);
      viewport.removeEventListener("touchend", onTouchEnd);
      viewport.removeEventListener("touchcancel", onTouchCancel);
    };
  }, [onWheel, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel]);
  return <div ref={viewportRef} data-testid="viewport" />;
}

describe("older history gestures at the top", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  function setup() {
    const loadPage = vi.fn(() => true);
    const view = renderWithBrowser(<Harness loadPage={loadPage} />);
    return { ...view, viewport: view.getByTestId("viewport"), loadPage };
  }

  function wheel(viewport: HTMLElement, deltaY: number, momentum?: boolean, extra: WheelEventInit = {}) {
    const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true, ...extra });
    Object.defineProperty(event, "timeStamp", { value: performance.now() });
    if (extra.ctrlKey !== undefined) Object.defineProperty(event, "ctrlKey", { value: extra.ctrlKey });
    if (momentum !== undefined) Object.defineProperty(event, "momentum", { value: momentum });
    act(() => {
      viewport.dispatchEvent(event);
      if (!event.defaultPrevented && !event.ctrlKey && Math.abs(event.deltaX) <= Math.abs(deltaY)) {
        viewport.scrollTop = Math.max(0, viewport.scrollTop + deltaY);
      }
    });
    return event;
  }

  function pause() { act(() => { vi.advanceTimersByTime(150); }); }

  function touch(viewport: HTMLElement, type: string, y?: number, fingers = 1) {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, "touches", { value: y === undefined ? [] : Array.from({ length: fingers }, () => ({ clientY: y })) });
    act(() => { viewport.dispatchEvent(event); });
    return event;
  }

  it("allows native arrival but requires a separate upward burst to load", () => {
    const { viewport, loadPage } = setup();
    viewport.scrollTop = 120;
    expect(wheel(viewport, -120, false).defaultPrevented).toBe(false);
    expect(viewport.scrollTop).toBe(0);
    expect(wheel(viewport, -100, false).defaultPrevented).toBe(true);
    expect(wheel(viewport, -200, true).defaultPrevented).toBe(true);
    expect(loadPage).not.toHaveBeenCalled();
    pause();
    wheel(viewport, -100, false);
    expect(loadPage).toHaveBeenCalledOnce();
    expect(viewport.scrollTop).toBe(0);
  });

  it("keeps rows stationary while accumulating intent and across an empty or failed request", () => {
    const { viewport, loadPage } = setup();
    loadPage.mockReturnValue(false);
    expect(wheel(viewport, -32).defaultPrevented).toBe(true);
    expect(viewport.scrollTop).toBe(0);
    expect(loadPage).not.toHaveBeenCalled();
    expect(wheel(viewport, -32).defaultPrevented).toBe(true);
    expect(loadPage).toHaveBeenCalledOnce();
    expect(viewport.scrollTop).toBe(0);
    wheel(viewport, -100);
    expect(loadPage).toHaveBeenCalledOnce();
    pause();
    wheel(viewport, -100);
    expect(loadPage).toHaveBeenCalledTimes(2);
  });

  it("ignores the rest of the loading gesture after a fast page prepend", () => {
    const { viewport, loadPage } = setup();
    wheel(viewport, -80, false);
    viewport.scrollTop = 300;
    expect(wheel(viewport, -40, true).defaultPrevented).toBe(true);
    expect(wheel(viewport, -40, false).defaultPrevented).toBe(true);
    expect(viewport.scrollTop).toBe(300);
    expect(loadPage).toHaveBeenCalledOnce();
    pause();
    expect(wheel(viewport, -40, false).defaultPrevented).toBe(false);
    expect(viewport.scrollTop).toBe(260);
  });

  it("drops partial wheel input on switch, and skips a request when no history is available", () => {
    const { viewport, loadPage, rerender } = setup();
    wheel(viewport, -40);
    rerender(<Harness thread="other" loadPage={loadPage} />);
    wheel(viewport, -40);
    expect(loadPage).not.toHaveBeenCalled();
    rerender(<Harness thread="other" loadPage={loadPage} available={false} />);
    wheel(viewport, -40);
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("ignores zoom, horizontal input and inertia at the top", () => {
    const { viewport, loadPage } = setup();
    expect(wheel(viewport, -120, false, { ctrlKey: true }).defaultPrevented).toBe(false);
    expect(wheel(viewport, -120, false, { deltaX: 200 }).defaultPrevented).toBe(false);
    expect(wheel(viewport, -120, true).defaultPrevented).toBe(true);
    expect(loadPage).not.toHaveBeenCalled();
  });

  it("holds touch input until release and does not move the list", () => {
    const { viewport, loadPage } = setup();
    viewport.scrollTop = 120;
    touch(viewport, "touchstart", 100);
    viewport.scrollTop = 0;
    touch(viewport, "touchmove", 250);
    touch(viewport, "touchend");
    expect(loadPage).not.toHaveBeenCalled();
    touch(viewport, "touchstart", 100);
    expect(touch(viewport, "touchmove", 140).defaultPrevented).toBe(true);
    touch(viewport, "touchmove", 180);
    expect(viewport.scrollTop).toBe(0);
    expect(loadPage).not.toHaveBeenCalled();
    touch(viewport, "touchend");
    expect(loadPage).toHaveBeenCalledOnce();
  });

  it.each(["touchcancel", "multitouch", "reversal"])("cancels touch intent on %s", (type) => {
    const { viewport, loadPage } = setup();
    touch(viewport, "touchstart", 100);
    touch(viewport, "touchmove", 220);
    if (type === "multitouch") touch(viewport, "touchmove", 240, 2);
    else if (type === "reversal") touch(viewport, "touchmove", 90);
    else touch(viewport, type);
    touch(viewport, "touchend");
    expect(loadPage).not.toHaveBeenCalled();
  });
});
