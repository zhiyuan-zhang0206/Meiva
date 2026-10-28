import { render as testingLibraryRender, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

import { BrowserProvider } from "../../browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "../../browser/testing";
import { WallClockProvider } from "../transport-context";

const realWallNow = () => Date.now();

// meiva:note
// Testing Library's `render` with the two values a timeline row reads: the page
// (through <BrowserProvider>) and the wall clock (through <WallClockProvider>).
// The components under test are not about time or navigation, so both are the
// plain ones — a test that wants an instant or an address of its own mounts the
// provider itself.
export function render(
  ui: ReactElement,
  options?: Omit<RenderOptions, "queries"> & { browser?: FakeBrowser },
): RenderResult {
  const Outer = options?.wrapper;
  const browser = options?.browser ?? fakeBrowser();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <BrowserProvider browser={browser}>
        <WallClockProvider wallNow={realWallNow}>
          {Outer === undefined ? children : <Outer>{children}</Outer>}
        </WallClockProvider>
      </BrowserProvider>
    );
  }
  return testingLibraryRender(ui, { ...options, wrapper: Wrapper });
}
