// meiva:note
// The timing event exists so a slow navigation can be diagnosed from the tab that
// felt it: the click stamp, the route change, the first frame after, the longest
// task and how many frames the browser actually produced while the route was
// stuck. The fallback is the floor under a navigation that never commits.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let pathname = "/";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

const tracked: { element: string; key?: string; value?: unknown; page?: string }[] = [];
vi.mock("./telemetry", () => ({
  track: (element: string, opts: { key?: string; value?: unknown; page?: string }) =>
    tracked.push({ element, key: opts.key, value: opts.value, page: opts.page }),
}));

import { formatNavTiming, markNavIntent, setNavFallbackMs, useNavTimingReport } from "./nav-timing";

function Reporter() {
  useNavTimingReport();
  return null;
}

const frames: FrameRequestCallback[] = [];
function flushFrame() {
  const next = frames.shift();
  next?.(0);
}

describe("nav timing", () => {
  beforeEach(() => {
    tracked.length = 0;
    frames.length = 0;
    pathname = "/";
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.push(cb);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("formats the three numbers as one bounded scalar", () => {
    expect(formatNavTiming(412.6, 987.2, 233.4)).toBe("c=413|p=987|t=233");
  });

  it("reports nothing when no click stamped an intent", () => {
    const { rerender } = render(<Reporter />);
    pathname = "/control";
    rerender(<Reporter />);
    expect(tracked).toEqual([]);
  });

  it("reports click-to-commit, click-to-paint and the longest task", async () => {
    const { rerender } = render(<Reporter />);
    markNavIntent();
    pathname = "/control";
    rerender(<Reporter />);
    flushFrame();
    flushFrame();
    await new Promise((resolve) => setTimeout(resolve, 600));
    const nav = tracked.filter((e) => e.key === "nav");
    expect(nav).toHaveLength(1);
    expect(nav[0].element).toBe("nav-timing");
    expect(nav[0].page).toBe("control");
    expect(nav[0].value).toMatch(/^c=\d+\|p=\d+\|t=\d+$/);
  });

  it("waits the configured time before falling back", () => {
    vi.useFakeTimers();
    const assign = vi.fn();
    const realLocation = window.location;
    Object.defineProperty(window, "location", { configurable: true, value: { pathname: "/", assign } });
    try {
      setNavFallbackMs(500);
      markNavIntent("/control");
      vi.advanceTimersByTime(400);
      expect(assign).not.toHaveBeenCalled();
      vi.advanceTimersByTime(200);
      expect(assign).toHaveBeenCalledWith("/control");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: realLocation });
      vi.useRealTimers();
      setNavFallbackMs(2000);
    }
  });

  it("performs a real location change when the router never commits", () => {
    vi.useFakeTimers();
    const assign = vi.fn();
    const realLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/", assign },
    });
    try {
      markNavIntent("/control");
      vi.advanceTimersByTime(2100);
      expect(assign).toHaveBeenCalledWith("/control");
      const fallback = tracked.find((e) => e.key === "fallback");
      expect(fallback?.value).toMatch(/\|f=1\|fr=\d+\|v=[01]$/);
      expect(tracked.some((e) => e.key === "res")).toBe(true);
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: realLocation,
      });
      vi.useRealTimers();
    }
  });
});
