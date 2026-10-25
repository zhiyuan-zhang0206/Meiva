// meiva:note
// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  readContextPanelOpen,
  setContextPanelStorageForTest,
  useContextPanelOpen,
} from "./context-panel-open";
import { act, renderHook } from "@testing-library/react";

const KEY = "meiva.home.contextPanelOpen";

describe("context panel open/closed is per device", () => {
  beforeEach(() => {
    localStorage.clear();
    setContextPanelStorageForTest(undefined);
  });

  afterEach(() => {
    setContextPanelStorageForTest(undefined);
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
    setContextPanelStorageForTest({
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(readContextPanelOpen()).toBe(true);
    const { result } = renderHook(() => useContextPanelOpen());
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
  });
});
