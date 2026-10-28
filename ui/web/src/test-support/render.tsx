// meiva:note
// `render` with the transport the tests control: the page mounts under the same
// <ApiProvider> the app uses, holding the fake from `fake-api`. A test file imports
// this as `render` (or keeps its own wrapper, which then nests inside the provider),
// so no test replaces the transport module.
import type { ReactElement, ReactNode } from "react";
import { render as rtlRender, type RenderOptions, type RenderResult } from "@testing-library/react";

import { ApiTestProvider } from "./fake-api";

export function renderWithApi(ui: ReactElement, options?: RenderOptions): RenderResult {
  const Outer = options?.wrapper;
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ApiTestProvider>
        {Outer === undefined ? children : <Outer>{children}</Outer>}
      </ApiTestProvider>
    );
  }
  return rtlRender(ui, { ...options, wrapper: Wrapper });
}
