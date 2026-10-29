import { describe, expect, it, vi } from "vitest";

import { viewportStore } from "./viewport-store";

describe("viewportStore", () => {
  it("holds the viewport it was given and hands out a new object only when it moves", () => {
    const store = viewportStore({ from: 1, to: 2 });
    expect(store.get()).toEqual({ from: 1, to: 2 });
    const before = store.get();
    store.set({ from: 1, to: 2 });
    expect(store.get()).toBe(before);
    store.set({ from: 1, to: 3 });
    expect(store.get()).toEqual({ from: 1, to: 3 });
    expect(store.get()).not.toBe(before);
  });

  it("wakes every listener on a move, and stays quiet when the bounds did not move", () => {
    const store = viewportStore({ from: 1, to: 2 });
    const seen = vi.fn();
    const stop = store.subscribe(seen);
    store.set({ from: 1, to: 2 });
    expect(seen).not.toHaveBeenCalled();
    store.set({ from: 2, to: 4 });
    expect(seen).toHaveBeenCalledTimes(1);
    stop();
    store.set({ from: 3, to: 6 });
    expect(seen).toHaveBeenCalledTimes(1);
  });
});
