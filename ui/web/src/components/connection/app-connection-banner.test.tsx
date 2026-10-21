// meiva:note
// AppConnectionBanner owns the "is this drop worth telling the user about" delay: a
// merged-stream reconnect on every agent switch must not flash the banner, a socket
// that stops retrying must show at once, and recovery must cancel a pending banner.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const states: string[] = [];
let emit: ((ev: { type: string }) => void) | null = null;

vi.mock("@/lib/auth/auth-context", () => ({
  useAuth: () => ({ status: "authenticated" }),
}));

vi.mock("@/lib/state/store", () => ({
  useStore: (selector: (s: { setConnState: (v: string) => void }) => unknown) =>
    selector({ setConnState: (v: string) => states.push(v) }),
}));

vi.mock("@/lib/notifications/use-cluster-health", () => ({
  useClusterHealth: () => undefined,
}));

vi.mock("@/lib/transport/useEventStream", () => ({
  useEventStream: (_onEvent: unknown, onConnection: (ev: { type: string }) => void) => {
    emit = onConnection;
  },
}));

import { AppConnectionBanner } from "./app-connection-banner";
import { NOTICE_DELAY_MS } from "@/components/notifications/connection-notice";

beforeEach(() => {
  states.length = 0;
  emit = null;
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("AppConnectionBanner connection state", () => {
  it("does not announce a reconnect that finishes inside the delay", () => {
    render(<AppConnectionBanner />);
    act(() => emit?.({ type: "reconnecting" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS - 50);
    });
    act(() => emit?.({ type: "open" }));
    act(() => {
      vi.advanceTimersByTime(NOTICE_DELAY_MS * 2);
    });
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
