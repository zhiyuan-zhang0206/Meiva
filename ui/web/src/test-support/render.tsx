// meiva:note
// The test door to a rendered component's page: every component that reaches the
// browser takes it from <BrowserProvider>, and a test that mounts nothing mounts
// no page — `useBrowser()` throws by design. These helpers mount the provider
// around the component under test with a fake browser, and hand back both the
// render result and the browser, so an assertion reads what the component asked
// the page to do (`browser.actions`) or what it stored.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import {
  render as rtlRender,
  renderHook as rtlRenderHook,
  type RenderHookOptions,
  type RenderOptions,
} from "@testing-library/react";
import type { ComponentType, ReactElement, ReactNode } from "react";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";

type Wrapper = ComponentType<{ children: ReactNode }>;

function wrap(browser: FakeBrowser, inner: Wrapper | undefined): Wrapper {
  const Inner = inner;
  return function BrowserWrapper({ children }: { children: ReactNode }): ReactElement {
    return (
      <BrowserProvider browser={browser}>
        {Inner === undefined ? children : <Inner>{children}</Inner>}
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
