// meiva:note
// The test doors to a rendered component's page: a component that reaches the browser takes
// it from <BrowserProvider>, one that talks to the gateway takes its client from
// <ApiProvider>. Both helpers mount the same two providers the app mounts -- a fake browser
// and the fake transport -- and nest a caller's own wrapper inside them, so a test composes
// the real tree with fakes and no test replaces a module.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
import {
  render as rtlRender,
  renderHook as rtlRenderHook,
  type RenderHookOptions,
  type RenderOptions,
  type RenderResult,
} from "@testing-library/react";
import type { ComponentType, ReactElement, ReactNode } from "react";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";

import { ApiTestProvider } from "./fake-api";

type Wrapper = ComponentType<{ children: ReactNode }>;

function wrap(browser: FakeBrowser, inner: Wrapper | undefined): Wrapper {
  const Inner = inner;
  return function BrowserApiWrapper({ children }: { children: ReactNode }): ReactElement {
    return (
      <BrowserProvider browser={browser}>
        <ApiTestProvider>{Inner === undefined ? children : <Inner>{children}</Inner>}</ApiTestProvider>
      </BrowserProvider>
    );
  };
}

export interface BrowserRenderOptions extends Omit<RenderOptions, "wrapper"> {
  browser?: FakeBrowser;
  wrapper?: Wrapper;
}

export function renderWithBrowser(
  ui: ReactElement,
  { browser = fakeBrowser(), wrapper, ...options }: BrowserRenderOptions = {},
) {
  const result = rtlRender(ui, { ...options, wrapper: wrap(browser, wrapper) });
  return { ...result, browser };
}

export interface BrowserHookOptions<T> extends Omit<RenderHookOptions<T>, "wrapper"> {
  browser?: FakeBrowser;
  wrapper?: Wrapper;
}

export function renderHookWithBrowser<T>(
  hook: () => T,
  { browser = fakeBrowser(), wrapper, ...options }: BrowserHookOptions<T> = {},
) {
  const result = rtlRenderHook(hook, { ...options, wrapper: wrap(browser, wrapper) });
  return { ...result, browser };
}

// meiva:note
// `render` with the transport the tests control: a test file imports this as `render` (or
// keeps its own wrapper, which then nests inside the provider), so no test replaces the
// transport module.
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
