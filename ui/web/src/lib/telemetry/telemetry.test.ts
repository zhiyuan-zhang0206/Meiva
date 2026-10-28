// meiva:note
// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeBrowser } from "../browser/testing";
import { API_BASE } from "../transport/api-base";
import {
  initTelemetry,
  normalizePage,
  setTelemetryPage,
  telemetryTestControl,
  track,
} from "./telemetry";

// Note: the module starts enabled because `typeof window !== "undefined"`
// under happy-dom — the SSR no-op path is exercised via the disable flag.

function stubBeacon(impl?: (url: string, body: string) => boolean) {
  const sendBeacon = vi.fn(impl ?? (() => true));
  initTelemetry({
    ...fakeBrowser(),
    beacon: { send: (url, body) => sendBeacon(url, body) },
  });
  return sendBeacon;
}

/** One tracked interaction as serialized on the wire. */
interface BeaconEvent {
  page: string;
  element: string;
  key?: string;
  value?: string;
  ts: number;
}

interface BeaconBody {
  session_id: string;
  events: BeaconEvent[];
}

/** Parse the JSON payload of the first beacon call. */
function beaconBody(sendBeacon: ReturnType<typeof vi.fn>): BeaconBody {
  return JSON.parse(String(sendBeacon.mock.calls[0][1])) as BeaconBody;
}

// meiva:note
// A wall clock a test steps by hand: the module reads the injected function.
function steppedClock(start: number): { now: () => number; advance: (ms: number) => void } {
  let current = start;
  return {
    now: () => current,
    advance: (ms) => {
      current += ms;
    },
  };
}

beforeEach(() => {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  telemetryTestControl.reset();
});

afterEach(() => {
  telemetryTestControl.reset();
  vi.restoreAllMocks();
});

describe("normalizePage", () => {
  it("maps the root route to home", () => {
    expect(normalizePage("/")).toBe("home");
  });
  it("keeps nested routes", () => {
    expect(normalizePage("/control/config")).toBe("control/config");
    expect(normalizePage("/insights/ops")).toBe("insights/ops");
  });
  it("collapses agent ids in shell routes", () => {
    expect(normalizePage("/shell/42")).toBe("shell");
    expect(normalizePage("/shell/42/")).toBe("shell");
  });
  it("strips leading slashes", () => {
    expect(normalizePage("/fleet")).toBe("fleet");
  });
});

describe("track + flush", () => {
  it("buffers then flushes one batch with the session id", () => {
    const sendBeacon = stubBeacon();
    track("spawn");
    expect(telemetryTestControl.bufferSize()).toBe(1);
    expect(telemetryTestControl.flush()).toBe(1);
    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, payload] = sendBeacon.mock.calls[0];
    expect(url).toBe(`${API_BASE}/api/frontend-telemetry`);
    expect(JSON.parse(payload)).toBeTypeOf("object");
    const body = beaconBody(sendBeacon);
    expect(body.session_id).toMatch(/^[0-9a-f-]{8,64}$/);
    expect(body.events).toEqual([
      { page: "unknown", element: "spawn", ts: expect.any(Number) as number },
    ]);
  });

  it("inherits the current page from setTelemetryPage", () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("fleet");
    track("spawn");
    expect(telemetryTestControl.flush()).toBe(1);
    const body = beaconBody(sendBeacon);
    expect(body.events[0].page).toBe("fleet");
  });

  it("falls back to fetch+keepalive when sendBeacon is unavailable", async () => {
    stubBeacon().mockImplementation(() => false);
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(
      () => Promise.resolve(new Response(null, { status: 204 })),
    );
    telemetryTestControl.setFetch(fetchMock);
    track("page-view");
    telemetryTestControl.flush();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE}/api/frontend-telemetry`);
    expect(init?.method).toBe("POST");
    expect(init?.keepalive).toBe(true);
    expect(init?.credentials).toBe("include");
  });

  it("drops nothing when flush finds an empty buffer", () => {
    const sendBeacon = stubBeacon();
    expect(telemetryTestControl.flush()).toBe(0);
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it("flushes buffered metrics when the document becomes hidden", () => {
    const sendBeacon = stubBeacon();
    track("web-vitals", { key: "lcp", value: 900, dedupe: false });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });

    document.dispatchEvent(new Event("visibilitychange"));

    expect(sendBeacon).toHaveBeenCalledTimes(1);
  });
});

describe("dedupe", () => {
  it("suppresses the same (page, element, key) within 2 s", () => {
    stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_timestamp_weekday", value: false });
    track("setting-change", { key: "display.show_timestamp_weekday", value: true });
    track("setting-change", { key: "display.show_timestamp_weekday", value: false });
    expect(telemetryTestControl.bufferSize()).toBe(1);
  });

  it("does not dedupe different keys or elements", () => {
    stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_timestamp_weekday", value: false });
    track("setting-change", { key: "display.language", value: "zh" });
    track("spawn");
    expect(telemetryTestControl.bufferSize()).toBe(3);
  });

  it("tracks again after the dedupe window", () => {
    stubBeacon();
    const clock = steppedClock(1_000_000);
    telemetryTestControl.setWallNow(clock.now);
    track("spawn");
    clock.advance(1_000);
    track("spawn");
    expect(telemetryTestControl.bufferSize()).toBe(1);
    clock.advance(1_500);
    track("spawn");
    expect(telemetryTestControl.bufferSize()).toBe(2);
  });

  it("keeps repeated samples when dedupe is disabled", () => {
    stubBeacon();
    track("web-vitals", { key: "lcp", value: 900, dedupe: false });
    track("web-vitals", { key: "lcp", value: 950, dedupe: false });
    expect(telemetryTestControl.bufferSize()).toBe(2);
  });
});

describe("rate limit", () => {
  it("caps pending + sent at 100 events per minute", () => {
    stubBeacon();
    for (let i = 0; i < 105; i++) track("page-view", { page: `p${i}` });
    expect(telemetryTestControl.bufferSize()).toBe(100);
  });

  it("resets the budget after the window elapses", () => {
    stubBeacon();
    const clock = steppedClock(1_000_000);
    telemetryTestControl.setWallNow(clock.now);
    for (let i = 0; i < 100; i++) track("page-view", { page: `p${i}` });
    // flush sends them — budget spent
    telemetryTestControl.flush();
    track("page-view", { page: "after" });
    expect(telemetryTestControl.bufferSize()).toBe(0);
    clock.advance(61_000);
    track("page-view", { page: "later" });
    expect(telemetryTestControl.bufferSize()).toBe(1);
  });

  it("still caps events when dedupe is disabled", () => {
    stubBeacon();
    for (let i = 0; i < 105; i++) {
      track("web-vitals", { key: "lcp", value: i, dedupe: false });
    }
    expect(telemetryTestControl.bufferSize()).toBe(100);
  });
});

describe("instrumentation vocabulary", () => {
  it("accepts performance telemetry elements", () => {
    const sendBeacon = stubBeacon();
    track("api-timing", { key: "status", value: 801, dedupe: false });
    track("composer-latency", { key: "send-to-turn-start", value: 120 });
    track("web-vitals", { key: "fcp", value: 450, dedupe: false });

    expect(telemetryTestControl.flush()).toBe(3);
    const body = beaconBody(sendBeacon);
    expect(body.events.map((event) => event.element)).toEqual([
      "api-timing",
      "composer-latency",
      "web-vitals",
    ]);
  });
});

describe("value sanitization", () => {
  it("serializes booleans and numbers", () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.show_timestamp_weekday", value: false });
    track("setting-change", { key: "display.timeline_width_ratio", value: 0.4 });
    expect(telemetryTestControl.flush()).toBe(2);
    const body = beaconBody(sendBeacon);
    const byKey = Object.fromEntries(
      body.events
        .filter((e): e is BeaconEvent & { key: string; value: string } => e.key !== undefined)
        .map((e) => [e.key, e.value]),
    );
    expect(byKey["display.show_timestamp_weekday"]).toBe("false");
    expect(byKey["display.timeline_width_ratio"]).toBe("0.4");
  });

  it("truncates long strings to 64 chars", () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.custom", value: "x".repeat(200) });
    expect(telemetryTestControl.flush()).toBe(1);
    const body = beaconBody(sendBeacon);
    expect(body.events[0].value).toBe("x".repeat(64));
  });

  it("omits the value for non-scalar payloads", () => {
    const sendBeacon = stubBeacon();
    setTelemetryPage("control/config");
    track("setting-change", { key: "display.sidebar_sort", value: { key: "id" } });
    expect(telemetryTestControl.flush()).toBe(1);
    const body = beaconBody(sendBeacon);
    expect("value" in body.events[0]).toBe(false);
  });
});

describe("disabled / SSR", () => {
  it("is a no-op when disabled (SSR-safe path)", () => {
    const sendBeacon = stubBeacon();
    telemetryTestControl.setEnabled(false);
    track("spawn");
    expect(telemetryTestControl.bufferSize()).toBe(0);
    expect(telemetryTestControl.flush()).toBe(0);
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it("mints a stable session id per tab", () => {
    const a = telemetryTestControl.sessionId();
    const b = telemetryTestControl.sessionId();
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{8,64}$/);
  });
});

describe("event time", () => {
  it("stamps each event with the injected wall clock", () => {
    const sendBeacon = stubBeacon();
    const clock = steppedClock(1_700_000_000_000);
    telemetryTestControl.setWallNow(clock.now);
    track("spawn");
    clock.advance(5_000);
    track("page-view", { page: "control/config" });
    telemetryTestControl.flush();
    const body = beaconBody(sendBeacon);
    expect(body.events.map((event) => event.ts)).toEqual([1_700_000_000_000, 1_700_000_005_000]);
  });
});
