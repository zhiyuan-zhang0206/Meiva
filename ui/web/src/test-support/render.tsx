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
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentType, ReactElement, ReactNode } from "react";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import { USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { WallClockProvider } from "@/lib/transport/transport-context";

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

// meiva:note
// What `renderWithProviders` takes: `browser` is the page the component reads (one fake per
// render unless the caller shares one); `settings` is what the ["user-settings"] cache holds,
// defaults merged underneath, and a test that must prove the fetch path leaves it unset and
// answers `transport.getSettings` instead; `queryClient` lets a test share or inspect the
// cache, which is fresh per render otherwise; `wallNow` is the clock for a surface that reads
// one (`useWallNow`).
export interface ProvidersRenderOptions extends Omit<RenderOptions, "wrapper"> {
  browser?: FakeBrowser;
  settings?: Record<string, unknown>;
  queryClient?: QueryClient;
  wallNow?: () => number;
  wrapper?: Wrapper;
}

// meiva:note
// The full test door: the same providers the app's composition root mounts -- browser,
// transport, wall clock and the query cache -- so a surface that reads server data or a
// preference is rendered with fakes and values instead of a replaced module. The cache is
// seeded with `settings` (defaults underneath) because the cache is the settings' owner;
// `putSetting` still goes to the fake transport, so a write is asserted where it happens.
// `browser` is built once here (a new fake per render would change the provider's identity
// every render) and returned, so a test can read what the component asked the page to do.
export function renderWithProviders(
  ui: ReactElement,
  {
    browser = fakeBrowser(),
    settings,
    queryClient,
    wallNow,
    wrapper,
    ...options
  }: ProvidersRenderOptions = {},
): RenderResult & { browser: FakeBrowser; queryClient: QueryClient } {
  const qc =
    queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, retryDelay: 0 }, mutations: { retry: false } },
    });
  if (settings !== undefined) {
    qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...settings });
  }
  const Outer = wrapper;
  function Wrapper({ children }: { children: ReactNode }): ReactElement {
    const inner =
      Outer === undefined ? children : <Outer>{children}</Outer>;
    const cached = <QueryClientProvider client={qc}>{inner}</QueryClientProvider>;
    return (
      <BrowserProvider browser={browser}>
        <ApiTestProvider>
          {wallNow === undefined ? cached : <WallClockProvider wallNow={wallNow}>{cached}</WallClockProvider>}
        </ApiTestProvider>
      </BrowserProvider>
    );
  }
  return { ...rtlRender(ui, { ...options, wrapper: Wrapper }), browser, queryClient: qc };
}
