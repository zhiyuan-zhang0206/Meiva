// Vitest setup — global stubs for things the test DOM (happy-dom) doesn't
// implement, plus a network guard (below) that makes unmocked real requests
// fail loud instead of silently hitting a real gateway.
//
// happy-dom ships a no-op IntersectionObserver whose callback never fires, so
// any component that gates work on visibility (the Control page's per-section
// data loading) would stay permanently "not visible" under test. Stub it to
// report every observed element as intersecting the moment it's observed, so
// section content loads deterministically. A test that needs the not-visible
// path installs its own stub via vi.stubGlobal.

/* eslint-disable @typescript-eslint/no-empty-function -- observer teardown methods are intentional no-ops in the stub */

import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// RTL auto-cleanup registers itself only when `afterEach` exists on
// globalThis — which `globals: false` (vitest.config.mts) never provides — so
// without this line NOTHING unmounts between tests. Rendered components leak
// for the rest of the file; a leaked live-clock interval (see
// src/components/timeline/reasoning-clock.ts useNow) can then fire a setState
// after the worker's happy-dom window is torn down, and react-dom's scheduler
// blows up with "ReferenceError: window is not defined" AFTER every test
// passed. Register the cleanup explicitly.
afterEach(cleanup);

class ImmediateIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null;
  readonly rootMargin: string = "";
  readonly scrollMargin: string = "";
  readonly thresholds: readonly number[] = [];
  private readonly callback: IntersectionObserverCallback;

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element): void {
    // Fire synchronously with isIntersecting=true — the element is treated as
    // fully on-screen, which is what the gating hook keys off.
    this.callback(
      [
        {
          isIntersecting: true,
          intersectionRatio: 1,
          target,
          boundingClientRect: target.getBoundingClientRect(),
          intersectionRect: target.getBoundingClientRect(),
          rootBounds: null,
          time: 0,
        },
      ],
      this,
    );
  }

  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

globalThis.IntersectionObserver = ImmediateIntersectionObserver;

// -- Global network guard ----------------------------------------------------
//
// Dev boxes run a real Meiva gateway on :8000. A test that forgets to mock
// @/lib/api (or @/lib/useEventStream for SSE) doesn't fail locally — it
// silently round-trips against that real gateway — and only breaks in CI,
// where nothing is listening. That gap let two real leaks (providers.test.tsx,
// status/page.test.tsx) sit unnoticed until PR #789's CI job hit a flood of
// ECONNREFUSED; see commit 31191bb5 for the bisection. Make every real
// network path throw immediately, everywhere, by default — a test that
// genuinely owns the network boundary (api.test.ts, useEventStream.test.ts,
// upload-XHR tests) installs its own vi.stubGlobal(...) override for the one
// call it's exercising.
//
// Installed via plain assignment, not vi.stubGlobal: vi.stubGlobal remembers
// the value as of its *first* call on a given key as "original", and
// vi.unstubAllGlobals() restores to that original. If the guard itself used
// vi.stubGlobal, a test's own beforeEach(stubGlobal)/afterEach(unstubAllGlobals)
// cycle would restore the *real, unguarded* fetch after that test — leaving
// every other test in the same file unprotected. Plain assignment here means
// the real fetch/XMLHttpRequest/EventSource never re-enters scope for the
// life of the test process; a test's own stubGlobal call records *this*
// guard as the value to restore to.

type GuardedKind = "fetch" | "XMLHttpRequest" | "EventSource";

// A tripped guard rejects/throws right at the call site, but most call sites
// are query hooks whose caller never awaits or asserts on that rejection
// (React Query's internal try/catch folds it into query.error state, which a
// render-only smoke test never looks at) — so the rejection alone can go
// unnoticed by the test that caused it, and by the time an unhandled
// rejection would surface (a query's later retry, after cleanup) vitest can
// misattribute it to whatever unrelated test happens to be running by then.
// Record every trip and fail it loudly, attributed to the right test, in a
// global afterEach instead of relying on that.
let networkGuardTrips: string[] = [];

function networkGuardError(kind: GuardedKind, target: string): Error {
  const message =
    `[network-guard] blocked a real ${kind} request to ${target} -- tests must provide ` +
    `an explicit transport or ApiProvider instead of hitting the network. If this ` +
    `test file legitimately owns the network boundary, inject its own ${kind} ` +
    `implementation (see src/lib/transport/tests).`;
  networkGuardTrips.push(message);
  return new Error(message);
}

afterEach(() => {
  if (networkGuardTrips.length === 0) return;
  const trips = networkGuardTrips;
  networkGuardTrips = [];
  throw new Error(
    `network-guard: ${trips.length} unmocked real network call(s) during this test:\n` +
      trips.join("\n"),
  );
});

// meiva:note
// The guards below replace the platform's fetch and XMLHttpRequest. A test that
// owns a real exchange with a local server (src/lib/transport/tests/network) takes
// the originals from this frozen record and hands them to its transport explicitly.
Object.defineProperty(globalThis, Symbol.for("meiva.test.realNetwork"), {
  value: Object.freeze({ fetch: globalThis.fetch, XMLHttpRequest: globalThis.XMLHttpRequest }),
});

globalThis.fetch = (input: RequestInfo | URL) => {
  const target =
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return Promise.reject(networkGuardError("fetch", target));
};

class GuardedXMLHttpRequest {
  // Minimal surface api.ts's uploadFiles() touches before send() — enough that
  // assigning to them doesn't throw for a caller that hasn't hit the guard yet.
  upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
  withCredentials = false;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private targetUrl = "(unknown — open() was never called)";

  open(_method: string, target: string | URL): void {
    this.targetUrl = String(target);
  }

  send(): void {
    // Real XMLHttpRequest.send() is synchronous-looking but async under the
    // hood; api.ts's only caller wraps it in `new Promise(...)`, which
    // catches a synchronous throw from the executor and turns it into a
    // rejection — matching guardedFetch's Promise.reject above.
    throw networkGuardError("XMLHttpRequest", this.targetUrl);
  }
}
globalThis.XMLHttpRequest = GuardedXMLHttpRequest as unknown as typeof XMLHttpRequest;

// happy-dom doesn't implement EventSource at all (constructing one today
// throws a bare "EventSource is not defined" ReferenceError) — this stub is
// defense in depth against a future Node/happy-dom version adding a real one,
// and gives the same URL + mock hint as the fetch/XHR guards above instead of
// an opaque ReferenceError.
function GuardedEventSource(target: string | URL): never {
  throw networkGuardError("EventSource", String(target));
}
globalThis.EventSource = GuardedEventSource as unknown as typeof EventSource;
