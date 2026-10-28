"use client";

// meiva:note
// useSettledAnchorScroll: the mount-time hash scroll plus the settle
// re-scroll. happy-dom has no ResizeObserver and no real layout, so the
// re-scroll path is exercised through a fake ResizeObserver whose trigger()
// runs the hook's callback, with the container's scrollHeight stubbed to
// simulate async content growth. The scroll itself is the browser port's, so a
// case reads what was scrolled where from the fake page instead of a DOM stub.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import { useSettledAnchorScroll } from "./anchor-scroll";

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  callback: ResizeObserverCallback;
  observed: Element | null = null;
  disconnected = false;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    FakeResizeObserver.instances.push(this);
  }
  observe(target: Element) {
    this.observed = target;
  }
  unobserve() {
    // meiva:note
    // noop — the hook only calls observe/disconnect.
  }
  disconnect() {
    this.disconnected = true;
  }
  trigger() {
    this.callback([], this);
  }
}

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

function mount(targetId: string | null) {
  const browser = fakeBrowser();
  const result = render(
    <BrowserProvider browser={browser}>
      <Harness targetId={targetId} />
    </BrowserProvider>,
  );
  const scrolled = () => browser.actions.scrolled;
  return { browser, scrolled, ...result };
}

function installFakeObserver(): FakeResizeObserver[] {
  const instances: FakeResizeObserver[] = (FakeResizeObserver.instances = []);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  return instances;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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
    const { scrolled } = mount(null);
    expect(scrolled()).toEqual([]);
  });

  it("re-scrolls when the content height changes, then stops once settled", () => {
    vi.useFakeTimers();
    try {
      const instances = installFakeObserver();
      const { container, scrolled } = mount("alerts");
      const scrollContainer = container.querySelector("#scroll")!;
      expect(instances).toHaveLength(1);
      const observer = instances[0];
      expect(observer.observed).toBe(container.querySelector("[data-testid=content]"));

      // meiva:note
      // The async content above the target grows — the observer fires and the
      // hook re-applies the scroll.
      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 4000,
      });
      observer.trigger();
      expect(scrolled()).toHaveLength(2);

      // meiva:note
      // A further height change within the settle window re-scrolls again.
      Object.defineProperty(scrollContainer, "scrollHeight", {
        configurable: true,
        value: 5000,
      });
      observer.trigger();
      expect(scrolled()).toHaveLength(3);

      // meiva:note
      // Once the height stays stable for the grace period the observer
      // detaches — the settle re-scroll is over.
      vi.advanceTimersByTime(600);
      expect(observer.disconnected).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops re-scrolling once the user takes over the scroll position", () => {
    const instances = installFakeObserver();
    const { container, scrolled } = mount("alerts");
    const scrollContainer = container.querySelector("#scroll")!;
    const observer = instances[0];

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
    observer.trigger();

    expect(observer.disconnected).toBe(true);
    // meiva:note
    // The user-takeover re-scroll is suppressed: only the mount scroll ran.
    expect(scrolled()).toHaveLength(1);
  });
});
