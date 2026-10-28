import { render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { fakeBrowser } from "./testing";
import { BrowserProvider, useBrowser } from "./browser-provider";

function wrapper(browser: ReturnType<typeof fakeBrowser>) {
  return ({ children }: { children: ReactNode }) => (
    <BrowserProvider browser={browser}>{children}</BrowserProvider>
  );
}

describe("BrowserProvider", () => {
  it("hands the component the browser the root mounted", () => {
    const browser = fakeBrowser({ href: "http://fake.local/shell/7#tasks" });
    const { result } = renderHook(() => useBrowser(), { wrapper: wrapper(browser) });
    expect(result.current.navigation.location()?.hash).toBe("#tasks");
    expect(result.current).toBe(browser);
  });

  it("throws outside a provider instead of falling back to the real window", () => {
    expect(() => renderHook(() => useBrowser())).toThrow(/useBrowser must be used inside/);
  });

  it("renders children with no browser of their own", () => {
    const { container } = render(
      <BrowserProvider browser={fakeBrowser()}>
        <span>child</span>
      </BrowserProvider>,
    );
    expect(container.textContent).toBe("child");
  });
});
