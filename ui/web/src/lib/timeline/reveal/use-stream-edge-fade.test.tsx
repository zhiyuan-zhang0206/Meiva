// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HighlightHost, HighlightLike } from "./edge-fade";
import { useStreamEdgeFade } from "./use-stream-edge-fade";

class FakeHost implements HighlightHost {
  readonly ranges = new Set<Range>();
  highlight(): HighlightLike {
    return this.ranges;
  }
  adoptStyles(): void {
    return undefined;
  }
}

function mount(text: string): { current: HTMLElement } {
  const el = document.createElement("div");
  el.textContent = text;
  document.body.append(el);
  return { current: el };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
});
afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("useStreamEdgeFade", () => {
  it("paints until the reveal catches up, then ages out, and clears on unmount", async () => {
    const host = new FakeHost();
    const ref = mount("a sentence that is being revealed");
    const { rerender, unmount } = renderHook(
      ({ caughtUp, shown }) => useStreamEdgeFade(ref, caughtUp, shown, host),
      { initialProps: { caughtUp: false, shown: "a sentence" } },
    );
    expect(host.ranges.size).toBeGreaterThan(0);
    rerender({ caughtUp: true, shown: "a sentence that is being revealed" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(host.ranges.size).toBeGreaterThan(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(host.ranges.size).toBe(0);
    rerender({ caughtUp: false, shown: "another burst" });
    expect(host.ranges.size).toBeGreaterThan(0);
    unmount();
    expect(host.ranges.size).toBe(0);
  });

  it("paints nothing without a host (no Highlight API, or reduced motion)", () => {
    const ref = mount("a sentence that is being revealed");
    const { unmount } = renderHook(() => useStreamEdgeFade(ref, false, "a sentence", null));
    unmount();
    expect(document.body.textContent).toBe("a sentence that is being revealed");
  });

  it("never paints a row that is already caught up (history)", () => {
    const host = new FakeHost();
    const ref = mount("history text");
    renderHook(() => useStreamEdgeFade(ref, true, "history text", host));
    expect(host.ranges.size).toBe(0);
  });
});
