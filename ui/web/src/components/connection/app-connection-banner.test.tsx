// meiva:note
// AppConnectionBanner owns the sustained-disconnect escalation delay:
// transient drops (reconnecting or closed) auto-retry silently via backoff;
// drops lasting >= CONNECTION_ERROR_DELAY_MS (15s) escalate directly to "closed",
// and a subsequent "open" immediately clears any pending timer and restores "open".
// meiva:link ui/web/src/docs/provider-composition.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render as rtlRender } from "@testing-library/react";
import { StrictMode, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-context";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import type { ConnectionState } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";
import { realNetwork, startGateway, type TestGateway } from "@/lib/transport/tests/api/api-provider";
import { gatewayRig, type HttpRig } from "@/lib/transport/tests/api/api-provider";
import { StreamHubProvider, ApiProvider } from "@/lib/transport/transport-context";

const clients = new Set<QueryClient>();
let stream: ReturnType<typeof fakeStreamHub>;
const states: string[] = [];

let gateway: TestGateway;
let rig: HttpRig;
const origSetConnState = useStore.getState().setConnState;
function ApiWrapper({ children }: { children: ReactNode }) {
  return <ApiProvider api={rig.api}>{children}</ApiProvider>;
}

import "@/lib/layout/use-document-visible";
import { EventStreamProvider } from "@/lib/transport/useEventStream";
import { StreamHub } from "@/lib/transport/stream/stream-hub";
import { FakeNetwork, FakeTab, READY } from "@/lib/transport/stream/tests/fake-stream";

import { AppConnectionBanner } from "./app-connection-banner";
import { fakeStreamHub, resetStreamHandlers, streamHandlers, TestStreamProvider } from "@/test-support/fake-stream";
import { CONNECTION_ERROR_DELAY_MS } from "@/components/notifications/connection-notice";

function render(ui: ReactElement, live = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  return rtlRender(
    <ApiWrapper>
      <BrowserProvider browser={fakeBrowser()}>
        <AuthProvider>
          <QueryClientProvider client={qc}>
            <StreamHubProvider hub={stream.hub}>
              {live ? <EventStreamProvider>{ui}</EventStreamProvider> : <TestStreamProvider>{ui}</TestStreamProvider>}
            </StreamHubProvider>
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
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  states.length = 0;
  stream = fakeStreamHub();
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
  stream.hub.dispose();
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
    expect(states).toEqual(["reconnecting", "open"]);

    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    act(() => streamHandlers.connection?.({ type: "open" }));
    expect(states).toEqual(["reconnecting", "open", "reconnecting", "open"]);

    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    view.unmount();
    expect(streamHandlers.connection).toBeUndefined();
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 2);
    });
    expect(states).toEqual(["reconnecting", "open", "reconnecting", "open", "reconnecting"]);
  });

  it("escalates sustained reconnecting to closed after 15s", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS - 100);
    });
    expect(states).toEqual(["reconnecting"]);

    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(states).toEqual(["reconnecting", "closed"]);
  });

  it("escalates sustained closed socket to closed after 15s grace window (not immediately)", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "closed" }));
    expect(states).toEqual(["reconnecting"]);

    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS);
    });
    expect(states).toEqual(["reconnecting", "closed"]);
  });

  it("subsequent open event immediately clears sustained closed state", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS);
    });
    expect(states).toEqual(["reconnecting", "closed"]);

    act(() => streamHandlers.connection?.({ type: "open" }));
    expect(states).toEqual(["reconnecting", "closed", "open"]);
    expect(useStore.getState().connState).toBe("open");
  });

  it("times out an initial connection that never opens or emits an error", async () => {
    await renderBanner();
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("closed");
  });

  it("retains the initial recovery deadline through StrictMode effect replay", async () => {
    render(<StrictMode><AppConnectionBanner /></StrictMode>);
    await vi.waitFor(() => expect(streamHandlers.connection).toBeDefined());
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("closed");
  });

  it("does not extend the deadline on repeated errors or visibility notifications", async () => {
    await renderBanner();
    act(() => { vi.advanceTimersByTime(10_000); });
    act(() => {
      streamHandlers.connection?.({ type: "closed" });
      stream.tab.page.show(true);
    });
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(useStore.getState().connState).toBe("closed");
  });

  it("cancels background escalation and gives return a fresh recovery window", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "open" }));
    act(() => streamHandlers.connection?.({ type: "reconnecting" }));
    act(() => { vi.advanceTimersByTime(10_000); });
    act(() => stream.tab.page.show(false));
    act(() => {
      streamHandlers.connection?.({ type: "closed" });
      vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 10);
    });
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => stream.tab.page.show(true));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS - 1); });
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => { vi.advanceTimersByTime(1); });
    expect(useStore.getState().connState).toBe("closed");
  });

  it("clears a previous outage on hide and stays silent after a quick return and open", async () => {
    await renderBanner();
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("closed");
    act(() => stream.tab.page.show(false));
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => stream.tab.page.show(true));
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => streamHandlers.connection?.({ type: "open" }));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
  });

  it("pauses visible background failures and immediately retries on window focus", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "open" }));
    const nonce = useStore.getState().reconnectNonce;
    act(() => stream.tab.page.focus(false));
    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 4); });
    expect(stream.tab.page.visibilityState).toBe("visible");
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => stream.tab.page.focus(true));
    expect(useStore.getState().reconnectNonce).toBe(nonce + 1);
    act(() => { vi.advanceTimersByTime(10_000); });
    act(() => stream.tab.page.focus(true));
    expect(useStore.getState().reconnectNonce).toBe(nonce + 1);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(useStore.getState().connState).toBe("closed");
  });

  it("keeps a healthy visible stream open through window focus changes", async () => {
    await renderBanner();
    act(() => streamHandlers.connection?.({ type: "open" }));
    const nonce = useStore.getState().reconnectNonce;
    act(() => stream.tab.page.focus(false));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 4); });
    act(() => stream.tab.page.focus(true));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
    expect(useStore.getState().reconnectNonce).toBe(nonce);
  });

  it("accepts automatic recovery while unfocused without another focus reconnect", async () => {
    await renderBanner();
    act(() => stream.tab.page.focus(false));
    act(() => streamHandlers.connection?.({ type: "closed" }));
    act(() => streamHandlers.connection?.({ type: "open" }));
    const nonce = useStore.getState().reconnectNonce;
    act(() => stream.tab.page.focus(true));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
    expect(useStore.getState().reconnectNonce).toBe(nonce);
  });

  it("preserves an open delivered before its visibility listener on return", async () => {
    stream.tab.page.addEventListener("visibilitychange", () => {
      if (stream.tab.page.visibilityState === "visible") streamHandlers.connection?.({ type: "open" });
    });
    await renderBanner();
    act(() => stream.tab.page.show(false));
    act(() => stream.tab.page.show(true));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
  });

  it.each(["legacy", "shared"] as const)("%s provider reopens on return and discards late old-source errors", async (capability) => {
    const tab = new FakeTab({ network: new FakeNetwork(), capability, wallNow: () => Date.now(), secureContext: true });
    const hub = new StreamHub(tab.transport, {
      checkAuth: () => Promise.resolve({ authenticated: true }),
      sessionInvalid: () => undefined,
    }, "");
    stream = { tab, hub };
    const show = (visible: boolean) => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: visible ? "visible" : "hidden" });
      tab.page.show(visible);
      document.dispatchEvent(new Event("visibilitychange"));
    };
    render(<AppConnectionBanner />, true);
    await vi.waitFor(() => expect(tab.sources.length).toBeGreaterThan(0));
    const currentSource = () => tab.sources.filter((source) => source.url.endsWith("/api/system")).at(-1)!;
    const oldSource = currentSource();
    act(() => oldSource.open());
    act(() => oldSource.error(READY.CONNECTING));
    act(() => show(false));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS * 4); });
    expect(useStore.getState().connState).toBe("reconnecting");
    await vi.waitFor(() => expect(oldSource.readyState).toBe(READY.CLOSED));
    act(() => show(true));
    await vi.waitFor(() => expect(currentSource()).not.toBe(oldSource));
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(useStore.getState().connState).toBe("reconnecting");
    act(() => currentSource().open());
    act(() => oldSource.error(READY.CLOSED));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
    const focusSource = currentSource();
    act(() => tab.page.focus(false));
    act(() => focusSource.error(READY.CONNECTING));
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(useStore.getState().connState).toBe("reconnecting");
    await act(async () => {
      tab.page.focus(true);
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(currentSource()).not.toBe(focusSource));
    act(() => currentSource().open());
    act(() => focusSource.error(READY.CLOSED));
    act(() => { vi.advanceTimersByTime(CONNECTION_ERROR_DELAY_MS); });
    expect(useStore.getState().connState).toBe("open");
  });

});
