// meiva:note
// AppConnectionBanner owns the "is this drop worth telling the user about" delay: a
// merged-stream reconnect on every agent switch must not flash the banner, a socket
// that stops retrying must show at once, and recovery must cancel a pending banner.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render as rtlRender } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-context";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import type { ConnectionState } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";
import { realNetwork, startGateway, type TestGateway } from "@/lib/transport/tests/api/api-provider";
import { gatewayRig, type HttpRig } from "@/lib/transport/tests/api/api-provider";
import { ApiProvider } from "@/lib/transport/transport-context";

const clients = new Set<QueryClient>();
const states: string[] = [];

let gateway: TestGateway;
let rig: HttpRig;
const origSetConnState = useStore.getState().setConnState;
function ApiWrapper({ children }: { children: ReactNode }) {
  return <ApiProvider api={rig.api}>{children}</ApiProvider>;
}

import { AppConnectionBanner } from "./app-connection-banner";
import { resetStreamHandlers, streamHandlers, TestStreamProvider } from "@/test-support/fake-stream";
import { NOTICE_DELAY_MS } from "@/components/notifications/connection-notice";

// meiva:note
// The banner mounts only once the session is authenticated, so the test renders the real
// AuthProvider over the same rig: the rig answers `/api/auth/check` and leaves every other call
// hanging, which is what the banner's own pollers then meet. Nothing replaces the auth module.
function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  return rtlRender(
    <ApiWrapper>
      <BrowserProvider browser={fakeBrowser()}>
        <AuthProvider>
          <QueryClientProvider client={qc}>
            <TestStreamProvider>{ui}</TestStreamProvider>
          </QueryClientProvider>
        </AuthProvider>
      </BrowserProvider>
    </ApiWrapper>,
  );
}

// meiva:note
// The session check is a promise the provider awaits on mount; flush it before driving the
// stream, which the banner subscribes to only after it renders.
async function renderBanner() {
  const view = render(<AppConnectionBanner />);
  await vi.waitFor(() => expect(streamHandlers.connection).toBeDefined());
  return view;
}

beforeEach(async () => {
  states.length = 0;
  resetStreamHandlers();
  gateway = await startGateway(realNetwork.createServer, (request) =>
    request.path === "/api/auth/check"
      ? { kind: "json", body: { authenticated: true } }
      : { kind: "hang" },
  );
  rig = gatewayRig(gateway);
  useStore.setState({
    connState: "open",
    setConnState: (v: ConnectionState) => {
      states.push(v);
      origSetConnState(v);
    },
  });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
});

afterEach(async () => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  useStore.setState({ connState: "open", setConnState: origSetConnState });
  vi.useRealTimers();
  await gateway.close();
});

describe("AppConnectionBanner connection state", () => {
  it("does not announce a reconnect that finishes inside the delay", async () => {
    const view = await renderBanner();
    await vi.waitFor(() =>
      expect(gateway.requests.map((request) => request.path)).toContain("/api/cluster/status"),
    );
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS - 50);
    });
    act(() => streamHandlers.connection?.({ type: "open" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS * 2);
    });
    expect(states).toEqual(["open"]);
    expect(gateway.requests.map((request) => request.path)).toEqual([
      "/api/auth/check",
      "/api/cluster/status",
    ]);
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    view.unmount();
    expect(streamHandlers.connection).toBeUndefined();
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
    act(() => { vi.advanceTimersByTime(NOTICE_DELAY_MS * 2); });
    expect(states).toEqual(["open"]);
  });

  it("announces a reconnect that outlasts the delay", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS);
    });
    expect(states).toEqual(["reconnecting"]);
  });

  it("announces a closed socket at once, with no wait", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "closed" }));
    expect(states).toEqual(["closed"]);
  });

  it("drops a pending reconnect banner when the socket closes", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS * 2);
    });
    expect(states).toEqual(["closed"]);
  });
});
