"use client";

// meiva:note
// useSettledAnchorScroll: the mount-time hash scroll plus the settle
// re-scroll. happy-dom has no layout engine, so the growth of the content
// wrapper is a value the fake page reports (`sizeWatches`) and the container's
// scrollHeight is stubbed to simulate async content arriving. The scroll itself
// is the browser port's, so a case reads what was scrolled where from the fake
// page instead of a DOM stub.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import { useSettledAnchorScroll } from "./anchor-scroll";

function Harness({ targetId }: { targetId: string | null }) {
  useSettledAnchorScroll("scroll", targetId);
  return (
    <div id="scroll">
      <div data-testid="content">
        <section id="alerts">Alerts</section>
      </div>
    </div>
  );
}

function mount(targetId: string | null, page: FakeBrowser = fakeBrowser()) {
  const result = render(
    <BrowserProvider browser={page}>
      <Harness targetId={targetId} />
    </BrowserProvider>,
  );
  const scrolled = () => page.actions.scrolled;
  return { page, scrolled, ...result };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(cleanup);

describe("useSettledAnchorScroll", () => {
  it("scrolls the resolved target into view on mount", () => {
    const { scrolled } = mount("alerts");
    expect(scrolled()).toHaveLength(1);
    expect((scrolled()[0].target as HTMLElement).id).toBe("alerts");
    expect(scrolled()[0].block).toBe("start");
  });

  it("does nothing for a null target", () => {
    const { page, scrolled } = mount(null);
    expect(scrolled()).toEqual([]);
    expect(page.sizeWatches).toEqual([]);
  });

  it("re-scrolls when the content height changes, then stops once settled", () => {
    vi.useFakeTimers();
    try {
      const page = fakeBrowser();
      const { container, scrolled } = mount("alerts", page);
      const scrollContainer = container.querySelector("#scroll")!;
      expect(page.sizeWatches).toHaveLength(1);
      const watch = page.sizeWatches[0];
      // meiva:note
      // The watch is on the content wrapper, not the container: only the
      // wrapper's height grows as the sections above the target arrive.
      expect(watch.target).toBe(container.querySelector("[data-testid=content]"));

      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 4000,
      });
      watch.fire();
      expect(scrolled()).toHaveLength(2);

      // meiva:note
      // A further height change within the settle window re-scrolls again.
      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 5000,
      });
      watch.fire();
      expect(scrolled()).toHaveLength(3);

      // meiva:note
      // Once the height stays stable for the grace period the watch ends —
      // the settle re-scroll is over.
      vi.advanceTimersByTime(600);
      expect(watch.released).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops re-scrolling once the user takes over the scroll position", () => {
    const page = fakeBrowser();
    const { container, scrolled } = mount("alerts", page);
    const scrollContainer = container.querySelector("#scroll")!;
    const watch = page.sizeWatches[0];

    // meiva:note
    // User scrolls during the settle window (the hook's lastScrollTop was 0).
    Object.defineProperty(scrollContainer, "scrollTop", {
      configurable: true,
      value: 400,
    });
    Object.defineProperty(scrollContainer, "scrollHeight", {
      configurable: true,
      value: 4000,
    });
    watch.fire();

    expect(watch.released).toBe(true);
    // meiva:note
    // The user-takeover re-scroll is suppressed: only the mount scroll ran.
    expect(scrolled()).toHaveLength(1);
  });
});
