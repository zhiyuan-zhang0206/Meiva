import { beforeEach, describe, expect, it, vi } from "vitest";

import { readContextPanelOpen, useContextPanelOpen } from "@/lib/context/context-panel-open";
import { act, renderHook } from "@testing-library/react";

const KEY = "meiva.home.contextPanelOpen";

describe("context panel open/closed is per device", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("defaults to open when nothing is stored", () => {
    expect(readContextPanelOpen()).toBe(true);
  });

  it("remembers a collapse on this device", () => {
    const { result } = renderHook(() => useContextPanelOpen());
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
    expect(localStorage.getItem(KEY)).toBe("0");
    expect(readContextPanelOpen()).toBe(false);
    act(() => result.current[1](true));
    expect(localStorage.getItem(KEY)).toBe("1");
  });

  it("still opens and closes when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readContextPanelOpen()).toBe(true);
    const { result } = renderHook(() => useContextPanelOpen());
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
  });
});
