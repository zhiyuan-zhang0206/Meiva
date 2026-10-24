import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect } from "vitest";

import type { Api } from "../../gateway/api-factory";
import { StreamHub } from "../../stream/stream-hub";
import { ApiProvider, StreamHubProvider } from "../../transport-context";
import { EventStreamProvider } from "../../useEventStream";
import { FakeNetwork, FakeTab, type FakeEventSource } from "../../stream/tests/fake-stream";

export interface EventStreamHarnessConfig {
  api: Api;
  queryClient: QueryClient;
  wallNow: () => number;
}

export interface EventStreamHarness {
  readonly tab: FakeTab;
  wrapper: ({ children }: { children: ReactNode }) => ReactNode;
  eventSource(): FakeEventSource;
  deliver(payload: unknown): void;
  open(): void;
  whenSourceExists(): Promise<void>;
}

// meiva:note
// A page whose global event stream is the real EventStreamProvider (and the real
// fold owner inside it) over a fake tab: the test delivers frames and opens the
// source by hand, and reads the gateway through the api it passes in. One legacy
// tab, so each page keeps its own EventSource.
export function eventStreamHarness(config: EventStreamHarnessConfig): EventStreamHarness {
  const tab = new FakeTab({
    network: new FakeNetwork(),
    capability: "legacy",
    wallNow: config.wallNow,
    secureContext: true,
  });
  const hub = new StreamHub(
    tab.transport,
    { checkAuth: () => Promise.resolve({ authenticated: true }), sessionInvalid: () => undefined },
    "",
  );
  const eventSource = (): FakeEventSource => {
    const source = tab.sources.at(-1);
    if (!source) throw new Error("no EventSource constructed yet");
    return source;
  };
  return {
    tab,
    wrapper: ({ children }) => (
      <ApiProvider api={config.api}>
        <StreamHubProvider hub={hub}>
          <QueryClientProvider client={config.queryClient}>
            <EventStreamProvider>{children}</EventStreamProvider>
          </QueryClientProvider>
        </StreamHubProvider>
      </ApiProvider>
    ),
    eventSource,
    deliver: (payload) => {
      act(() => eventSource().frame(JSON.stringify(payload)));
    },
    open: () => {
      act(() => eventSource().open());
    },
    whenSourceExists: () => waitFor(() => expect(tab.sources).not.toHaveLength(0)),
  };
}
