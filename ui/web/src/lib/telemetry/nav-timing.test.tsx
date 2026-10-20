// meiva:note
// The timing event exists so a slow navigation can be diagnosed from the tab that
// felt it: the click stamp, the route change, the first frame after and the
// longest task in between.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let pathname = "/";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

const tracked: { element: string; value?: unknown; page?: string }[] = [];
vi.mock("./telemetry", () => ({
  track: (element: string, opts: { value?: unknown; page?: string }) =>
    tracked.push({ element, value: opts.value, page: opts.page }),
}));

import { formatNavTiming, markNavIntent, useNavTimingReport } from "./nav-timing";

function Reporter() {
  useNavTimingReport();
  return null;
}

describe("nav timing", () => {
  beforeEach(() => {
    tracked.length = 0;
    pathname = "/";
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
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
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(tracked).toHaveLength(1);
    expect(tracked[0].element).toBe("nav-timing");
    expect(tracked[0].page).toBe("control");
    expect(tracked[0].value).toMatch(/^c=\d+\|p=\d+\|t=\d+$/);
  });
});
