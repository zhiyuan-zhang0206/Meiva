// meiva:note
// AppConnectionBanner owns the "is this drop worth telling the user about" delay: a
// merged-stream reconnect on every agent switch must not flash the banner, a socket
// that stops retrying must show at once, and recovery must cancel a pending banner.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render as rtlRender } from "@testing-library/react";
import { useEffect, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionState } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";
import { realNetwork, startGateway, type TestGateway } from "@/lib/transport/tests/api/api-provider";
import { gatewayRig, type HttpRig } from "@/lib/transport/tests/api/api-provider";
import { ApiProvider } from "@/lib/transport/transport-context";

const clients = new Set<QueryClient>();
const states: string[] = [];
let emit: ((ev: { type: string }) => void) | null = null;
let gateway: TestGateway;
let rig: HttpRig;
const origSetConnState = useStore.getState().setConnState;
function ApiWrapper({ children }: { children: ReactNode }) {
  return <ApiProvider api={rig.api}>{children}</ApiProvider>;
}

vi.mock("@/lib/auth/auth-context", () => ({
  useAuth: () => ({ status: "authenticated" }),
}));

vi.mock("@/lib/transport/useEventStream", () => ({
  useEventStream: (_onEvent: unknown, onConnection: (ev: { type: string }) => void) => {
    useEffect(() => {
      emit = onConnection;
      return () => { emit = null; };
    }, [onConnection]);
  },
}));

import { AppConnectionBanner } from "./app-connection-banner";
import { NOTICE_DELAY_MS } from "@/components/notifications/connection-notice";

function render(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  return rtlRender(<ApiWrapper><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ApiWrapper>);
}

beforeEach(async () => {
  states.length = 0;
  emit = null;
  gateway = await startGateway(realNetwork.createServer, () => ({ kind: "hang" }));
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
    const view = render(<AppConnectionBanner />);
    await vi.waitFor(() => expect(gateway.requests).toHaveLength(1));
    act(() => emit?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS - 50);
    });
    act(() => emit?.({ type: "open" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS * 2);
    });
    expect(states).toEqual(["open"]);
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/cluster/status"]);
    act(() => emit?.({ type: "reconnecting" }));
    view.unmount();
    expect(emit).toBeNull();
    for (const client of clients) {
      expect(client.getQueryCache().getAll().every((query) => query.getObserversCount() === 0)).toBe(true);
    }
    act(() => { vi.advanceTimersByTime(NOTICE_DELAY_MS * 2); });
    expect(states).toEqual(["open"]);
  });

  it("announces a reconnect that outlasts the delay", () => {
    render(<AppConnectionBanner />);
    act(() => emit?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS);
    });
    expect(states).toEqual(["reconnecting"]);
  });

  it("announces a closed socket at once, with no wait", () => {
    render(<AppConnectionBanner />);
    act(() => emit?.({ type: "closed" }));
    expect(states).toEqual(["closed"]);
  });

  it("drops a pending reconnect banner when the socket closes", () => {
    render(<AppConnectionBanner />);
    act(() => emit?.({ type: "reconnecting" }));
    act(() => emit?.({ type: "closed" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS * 2);
    });
    expect(states).toEqual(["closed"]);
  });
});
