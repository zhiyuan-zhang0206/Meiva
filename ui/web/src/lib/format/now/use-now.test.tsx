import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useNow } from "./use-now";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useNow", () => {
  it("starts at the wall clock it is given", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useNow(1_000, () => 1_000_000));
    expect(result.current.getTime()).toBe(1_000_000);
  });

  it("reads the wall clock on every tick, so a throttled tab catches up in one tick", () => {
    vi.useFakeTimers();
    let wall = 1_000_000;
    const readWall = () => wall;
    const { result } = renderHook(() => useNow(1_000, readWall));

    wall += 60_000;
    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(result.current.getTime()).toBe(1_060_000);
  });

  it("stops ticking when the component unmounts", () => {
    vi.useFakeTimers();
    let wall = 1_000_000;
    const readWall = () => wall;
    const { result, unmount } = renderHook(() => useNow(1_000, readWall));
    unmount();

    wall += 5_000;
    act(() => {
      vi.advanceTimersByTime(5_000);
    });

    expect(result.current.getTime()).toBe(1_000_000);
  });
});
