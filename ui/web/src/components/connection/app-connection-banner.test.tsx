// meiva:note
// AppConnectionBanner owns the sustained-disconnect escalation delay:
// transient drops (reconnecting or closed) auto-retry silently via backoff;
// drops lasting >= CONNECTION_ERROR_DELAY_MS (15s) escalate directly to "closed",
// and a subsequent "open" immediately clears any pending timer and restores "open".
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
import { CONNECTION_ERROR_DELAY_MS } from "@/components/notifications/connection-notice";

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
  it("does not announce reconnecting or closed drops that recover within 15s", async () => {
    const view = await renderBanner();
    await vi.waitFor(() =>
      expect(gateway.requests.map((request) => request.path)).toContain("/api/cluster/status"),
    );

    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    act(() => streamHandlers.connection?.({ type: "open" }));
    expect(states).toEqual(["open"]);

    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    act(() => streamHandlers.connection?.({ type: "open" }));
    expect(states).toEqual(["open", "open"]);

    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    view.unmount();
    expect(streamHandlers.connection).toBeUndefined();
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 2);
    });
    expect(states).toEqual(["open", "open"]);
  });

  it("escalates sustained reconnecting to closed after 15s", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS - 100);
    });
    expect(states).toEqual([]);

    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(states).toEqual(["closed"]);
  });

  it("escalates sustained closed socket to closed after 15s grace window (not immediately)", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "closed" }));
    expect(states).toEqual([]);

    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS);
    });
    expect(states).toEqual(["closed"]);
  });

  it("subsequent open event immediately clears sustained closed state", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS);
    });
    expect(states).toEqual(["closed"]);

    act(() => streamHandlers.connection?.({ type: "open" }));
    expect(states).toEqual(["closed", "open"]);
    expect(useStore.getState().connState).toBe("open");
  });
});
