import { render as testingLibraryRender, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

import { WallClockProvider } from "../transport-context";

const realWallNow = () => Date.now();

// meiva:note
// Testing Library's `render` with the page's wall clock provided: the components under test read the
// instant through `useWallNow`, and the tests that use this are not about time, so the clock is the
// real one. A test that wants an instant of its own provides `WallClockProvider` itself.
export function render(ui: ReactElement, options?: Omit<RenderOptions, "queries">): RenderResult {
  const Outer = options?.wrapper;
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <WallClockProvider wallNow={realWallNow}>
        {Outer === undefined ? children : <Outer>{children}</Outer>}
      </WallClockProvider>
    );
  }
  return testingLibraryRender(ui, { ...options, wrapper: Wrapper });
}
