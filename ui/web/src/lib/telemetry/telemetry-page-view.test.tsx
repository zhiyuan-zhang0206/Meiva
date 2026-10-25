// meiva:note
// @vitest-environment happy-dom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { telemetryTestControl } from "./telemetry";
import { TelemetryPageView } from "./telemetry-page-view";
import {
  webVitalsResetForTest,
  webVitalsSetRuntimeForTest,
  type PerformanceObserverConstructorLike,
} from "./web-vitals";

const observedTypes: string[] = [];

class CountingObserver {
  static supportedEntryTypes = ["paint"] as const;
  observe(options: PerformanceObserverInit): void {
    if (options.type) observedTypes.push(options.type);
  }
  disconnect(): void {
    return;
  }
  takeRecords(): PerformanceEntryList {
    return [];
  }
}

beforeEach(() => {
  observedTypes.length = 0;
  webVitalsResetForTest();
  webVitalsSetRuntimeForTest({
    observerConstructor: CountingObserver as unknown as PerformanceObserverConstructorLike,
  });
  telemetryTestControl.intercept({
    onTrack: () => undefined,
    onSetPage: () => undefined,
  });
});

afterEach(() => {
  cleanup();
  webVitalsResetForTest();
  webVitalsSetRuntimeForTest(null);
  telemetryTestControl.reset();
  vi.clearAllMocks();
});

it("initializes Web Vitals once across authenticated route changes", () => {
  const view = render(<TelemetryPageView pathname="/" />);
  view.rerender(<TelemetryPageView pathname="/fleet" />);

  expect(observedTypes).toEqual(["paint"]);
});
