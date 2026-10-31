"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useCallback, useState } from "react";

import { AppConnectionBanner } from "@/components/connection/app-connection-banner";
import { OpenTasksNoticeHost } from "@/components/notifications/open-tasks-notice";
import { ThemePackTokens } from "@/components/plugins/theme-pack-tokens";
import { ThemeColorSync } from "@/components/shell/theme-color-sync";
import { ToastHost } from "@/components/notifications/toast";
import { LanguageProvider } from "@/i18n/language-provider";
import { AuthProvider, notifySessionInvalid } from "@/lib/auth/auth-context";
import type { Browser } from "@/lib/browser/browser";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { browser as systemBrowser } from "@/lib/browser/system-browser";
import type { AppNavigation } from "@/lib/browser/navigation/navigation";
import { NavigationProvider } from "@/lib/browser/navigation/navigation-provider";
import { SystemNavigationProvider } from "@/lib/browser/navigation/system-navigation";
import { API_BASE, ApiError, api } from "@/lib/transport/api";
import type { Api } from "@/lib/transport/api";
import { NonceProvider } from "@/lib/auth/nonce-context";
import { StreamHub } from "@/lib/transport/stream/stream-hub";
import { systemStreamTransport } from "@/lib/transport/system-transport";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { EventStreamProvider } from "@/lib/transport/useEventStream";
import { initTelemetry } from "@/lib/telemetry/telemetry";
import { setNavFallbackMs, useNavTimingReport } from "@/lib/telemetry/nav-timing";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { AlertsProvider } from "@/lib/notifications/use-alerts";

// meiva:note
// The frontend's composition root wires the page into the two module-level
// singletons that cannot take a provider: telemetry (it flushes from a timer) and
// the transport's module-scope client. Components get the same value through
// <BrowserProvider> below.
initTelemetry(systemBrowser);

// The inline script next-themes injects toggles .dark on <html> before
// hydrate, no FOUC; a matchMedia listener follows system settings.
//
// attribute="class"      → use class, not data-attribute (matches shadcn)
// defaultTheme="system"  → follow system by default
// enableSystem           → allow "system" as a valid theme value
// disableTransitionOnChange → briefly suspend CSS transitions during a
//   theme swap to avoid color-transition flicker mid-swap
//
// QueryClientProvider: TanStack Query's global cache container; provides
// the data layer for hooks like useAgents (replaces the manual useState
// + setInterval polling pattern). useState holds the client instance —
// a module-level `new QueryClient()` would share one client across
// requests under SSR and leak data between users; useState gives each
// mount its own client (standard App Router approach).
//
// EventStreamProvider lives here, above the route tree, so the global
// `/api/system` broadcast survives page navigation (Main / Fleet / Settings /
// Memory share one EventSource instead of tearing it down and reopening on
// every switch; it does close while the tab is hidden — useEventStream.tsx).
// The fold owner (useFoldOwner inside EventStreamProvider) is the single root
// writer folding SSE into the query caches. The all-events, high-frequency
// stream stays scoped to the conversation view (page.tsx).
//
// AppConnectionBanner also rides that connection here at the root, so
// disconnect/recovery chrome protects every page, not just the home view.
export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (error instanceof ApiError && error.status === 401) {
          notifySessionInvalid();
        }
      },
    }),
    defaultOptions: {
      queries: {
        // Page switches should hit cache, not refetch: with the global
        // connection now persistent, a 5min staleTime keeps a just-visited
        // page's data warm across navigation (SSE-driven queries override
        // to Infinity; polling queries set their own refetchInterval).
        staleTime: 5 * 60_000,
        // Retain inactive (unobserved) query caches long enough that
        // returning to a page after a detour still hot-hits instead of
        // cold-fetching with a spinner.
        gcTime: 30 * 60_000,
        // Default-refetch-on-window-focus would shadow SSE-pushed
        // live data (e.g. timeline) with stray GETs; turn it off and
        // declare refetchInterval explicitly on useQueries that
        // actually want polling (e.g. status).
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          if (error instanceof ApiError) {
            // Task #1326: a 401 is not transient; retrying amplifies it into a poll storm.
            if (error.status === 401) return false;
            // Other 4xx answers are deterministic too — a retry re-hits the
            // same answer once per poll cadence. Only 408/429 ask for one.
            if (error.status < 500 && error.status !== 408 && error.status !== 429) {
              return false;
            }
          }
          // Task #3895: transient failures get at most two retries. Read paths
          // recover through their own polls / SSE invalidations, so a longer
          // burst against a struggling backend only multiplies load.
          return failureCount < 2;
        },
        // Capped exponential backoff (1s, 2s), so a failing request's retries
        // cannot spread past its poll interval into the next cycle.
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
      },
    },
  });
}

function RootContexts({
  children,
  nonce,
  client,
  hub,
  browser,
}: {
  children: React.ReactNode;
  nonce?: string;
  client: Api;
  hub: StreamHub;
  browser: Browser;
}) {
  const wallNow = useCallback(() => hub.transport.wallNow(), [hub]);
  return (
    <BrowserProvider browser={browser}>
      <ApiProvider api={client}>
        <StreamHubProvider hub={hub}>
          <WallClockProvider wallNow={wallNow}>
            <NonceProvider value={nonce}>{children}</NonceProvider>
          </WallClockProvider>
        </StreamHubProvider>
      </ApiProvider>
    </BrowserProvider>
  );
}

// meiva:note
// The navigation value the app's surfaces read: the real one comes from the app router, and a test
// that renders this tree hands in its own, the same way it hands in `client` and `browser`.
function NavigationRoot({
  navigation,
  children,
}: {
  navigation?: AppNavigation;
  children: React.ReactNode;
}) {
  return navigation === undefined ? (
    <SystemNavigationProvider>{children}</SystemNavigationProvider>
  ) : (
    <NavigationProvider navigation={navigation}>{children}</NavigationProvider>
  );
}

// meiva:note
// The composition root: it builds the app's one gateway client and hands it to every reader
// through <ApiProvider>. `client` names that input, so a test can compose the same tree with
// its own fake instead of replacing the transport module.
export function Providers({
  children,
  nonce,
  client = api,
  hub,
  browser = systemBrowser,
  navigation,
}: {
  children: React.ReactNode;
  nonce?: string;
  client?: Api;
  hub?: StreamHub;
  browser?: Browser;
  navigation?: AppNavigation;
}) {
  const [queryClient] = useState(() => createQueryClient());
  // meiva:note
  // What rides at the root, and why each is here rather than on a page: the plugin skin's tokens
  // over the active light/dark palette (`ThemePackTokens`, renders nothing), the theme colour sync,
  // the navigation timing report, the connection banner, the toast host (an error must reach the
  // user on every route, not just the Home page) and the terminate open-tasks notice (a terminate
  // that reports open tasks must surface wherever the action was taken).
  const [streamHub] = useState(
    () =>
      hub ??
      new StreamHub(
        systemStreamTransport(),
        { checkAuth: () => client.checkAuth(), sessionInvalid: notifySessionInvalid },
        API_BASE,
      ),
  );
  return (
    <NavigationRoot navigation={navigation}>
      <RootContexts nonce={nonce} client={client} hub={streamHub} browser={browser}>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <EventStreamProvider>
              <AlertsProvider>
                <ThemeProvider
                  attribute="class"
                  defaultTheme="system"
                  enableSystem
                  disableTransitionOnChange
                  nonce={nonce}
                >
                  <LanguageProvider>
                    <ThemePackTokens />
                    <ThemeColorSync />
                    <NavTimingReporter />
                    <AppConnectionBanner />
                    {children}
                    <ToastHost />
                    <OpenTasksNoticeHost />
                  </LanguageProvider>
                </ThemeProvider>
              </AlertsProvider>
            </EventStreamProvider>
          </AuthProvider>
        </QueryClientProvider>
      </RootContexts>
    </NavigationRoot>
  );
}

function NavTimingReporter() {
  const { settings } = useUserSettings();
  useNavTimingReport();
  setNavFallbackMs(Number(settings["behavior.navFallbackMs"] ?? 0));
  return null;
}
