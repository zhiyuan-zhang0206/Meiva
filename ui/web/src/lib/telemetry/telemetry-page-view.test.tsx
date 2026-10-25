// meiva:note
// @vitest-environment happy-dom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { telemetryTestControl } from "./telemetry";
import { TelemetryPageView } from "./telemetry-page-view";

beforeEach(() => {
  telemetryTestControl.intercept({
    onTrack: () => undefined,
    onSetPage: () => undefined,
  });
});

afterEach(() => {
  cleanup();
  telemetryTestControl.reset();
  vi.clearAllMocks();
});

it("initializes Web Vitals once across authenticated route changes", () => {
  const initSpy = vi.fn();
  const view = render(<TelemetryPageView pathname="/" onInitWebVitals={initSpy} />);
  view.rerender(<TelemetryPageView pathname="/fleet" onInitWebVitals={initSpy} />);

  expect(initSpy).toHaveBeenCalledTimes(1);
});
