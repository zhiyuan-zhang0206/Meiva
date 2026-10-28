// meiva:note
// The timing event exists so a slow navigation can be diagnosed from the tab that
// felt it: the click stamp, the route change, the first frame after, the longest
// task and how many frames the browser actually produced while the route was
// stuck. The fallback is the floor under a navigation that never commits.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

// meiva:note
// @vitest-environment happy-dom

import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";
import {
  formatNavTiming,
  markNavIntent,
  setNavFallbackMs,
  useNavTimingReport,
} from "./nav-timing";
import { telemetryTestControl } from "./telemetry";

const tracked: { element: string; key?: string; value?: unknown; page?: string }[] = [];

function Reporter({ pathname }: { pathname: string }) {
  useNavTimingReport(pathname);
  return null;
}

describe("nav timing", () => {
  beforeEach(() => {
    tracked.length = 0;
    telemetryTestControl.intercept({
      onTrack: (element, opts = {}) =>
        tracked.push({ element, key: opts.key, value: opts.value, page: opts.page }),
    });
  });
  afterEach(() => {
    telemetryTestControl.reset();
  });

  it("formats the three numbers as one bounded scalar", () => {
    expect(formatNavTiming(412.6, 987.2, 233.4)).toBe("c=413|p=987|t=233");
  });

  it("reports nothing when no click stamped an intent", () => {
    const { rerender } = render(<Reporter pathname="/" />);
    rerender(<Reporter pathname="/control" />);
    expect(tracked).toEqual([]);
  });

  it("reports click-to-commit, click-to-paint and the longest task", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(<Reporter pathname="/" />);
      markNavIntent(fakeBrowser().navigation);
      rerender(<Reporter pathname="/control" />);
      vi.advanceTimersByTime(600);
      const nav = tracked.filter((e) => e.key === "nav");
      expect(nav).toHaveLength(1);
      expect(nav[0].element).toBe("nav-timing");
      expect(nav[0].page).toBe("control");
      expect(nav[0].value).toMatch(/^c=\d+\|p=\d+\|t=\d+$/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("waits the configured time before falling back", () => {
    vi.useFakeTimers();
    const browser = fakeBrowser({ href: "http://fake.local/" });
    try {
      setNavFallbackMs(500);
      markNavIntent(browser.navigation, "/control");
      vi.advanceTimersByTime(400);
      expect(browser.actions.assigned).toEqual([]);
      vi.advanceTimersByTime(200);
      expect(browser.actions.assigned).toEqual(["/control"]);
    } finally {
      vi.useRealTimers();
      setNavFallbackMs(2000);
    }
  });

  it("performs a real location change when the router never commits", () => {
    vi.useFakeTimers();
    const browser = fakeBrowser({ href: "http://fake.local/" });
    try {
      markNavIntent(browser.navigation, "/control");
      vi.advanceTimersByTime(2100);
      expect(browser.actions.assigned).toEqual(["/control"]);
      const fallback = tracked.find((e) => e.key === "fallback");
      expect(fallback?.value).toMatch(/\|f=1\|fr=\d+\|v=[01]$/);
      expect(tracked.some((e) => e.key === "res")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
