// meiva:note
// @vitest-environment happy-dom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { telemetryTestControl } from "./telemetry";
import { TelemetryPageView } from "./telemetry-page-view";
import {
  webVitalsResetForTest,
  webVitalsSetRuntimeForTest,
} from "./web-vitals";

const register = vi.fn();

beforeEach(() => {
  register.mockReset();
  webVitalsResetForTest();
  webVitalsSetRuntimeForTest({
    onFCP: register, onLCP: register, onCLS: register, onINP: register,
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

  expect(register).toHaveBeenCalledTimes(4);
});
