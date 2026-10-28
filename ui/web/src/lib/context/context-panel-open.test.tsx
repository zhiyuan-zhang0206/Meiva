// meiva:note
// @vitest-environment happy-dom
//
// The panel's open/closed state lives in the browser port's storage, so each
// case is a storage answer: stored, absent, or unavailable. The hook is driven
// through <BrowserProvider> with a fake, the same way the app drives it.

import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import { readContextPanelOpen, useContextPanelOpen } from "./context-panel-open";

const KEY = "meiva.home.contextPanelOpen";

function open(initial: Record<string, string> = {}) {
  const browser = fakeBrowser({ local: initial });
  const { result } = renderHook(() => useContextPanelOpen(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <BrowserProvider browser={browser}>{children}</BrowserProvider>
    ),
  });
  return { browser, result };
}

describe("context panel open/closed is per device", () => {
  it("opens by default when this browser never stored a choice", () => {
    expect(readContextPanelOpen(fakeBrowser().local)).toBe(true);
  });

  it("reads a stored choice", () => {
    expect(readContextPanelOpen(fakeBrowser({ local: { [KEY]: "0" } }).local)).toBe(false);
    expect(readContextPanelOpen(fakeBrowser({ local: { [KEY]: "1" } }).local)).toBe(true);
  });

  it("remembers a collapse on this device", () => {
    const { browser, result } = open();
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
    expect(browser.localValues.get(KEY)).toBe("0");
    expect(readContextPanelOpen(browser.local)).toBe(false);
    act(() => result.current[1](true));
    expect(browser.localValues.get(KEY)).toBe("1");
  });

  it("still opens and closes when this frame has no storage", () => {
    const browser = fakeBrowser();
    browser.refuseStorage(true);
    expect(readContextPanelOpen(browser.local)).toBe(true);
    const { result } = renderHook(() => useContextPanelOpen(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <BrowserProvider browser={browser}>{children}</BrowserProvider>
      ),
    });
    act(() => result.current[1](false));
    expect(result.current[0]).toBe(false);
  });
});
