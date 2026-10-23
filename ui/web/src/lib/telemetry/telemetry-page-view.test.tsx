import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { telemetryTestControl } from "./telemetry";
import { TelemetryPageView } from "./telemetry-page-view";

let pathname = "/";

vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

beforeEach(() => {
  telemetryTestControl.intercept({
    onTrack: () => undefined,
    onSetPage: () => undefined,
  });
});

afterEach(() => {
  cleanup();
  pathname = "/";
  telemetryTestControl.reset();
  vi.clearAllMocks();
});

it("initializes Web Vitals once across authenticated route changes", () => {
  const initSpy = vi.fn();
  const view = render(<TelemetryPageView onInitWebVitals={initSpy} />);
  pathname = "/fleet";
  view.rerender(<TelemetryPageView onInitWebVitals={initSpy} />);

  expect(initSpy).toHaveBeenCalledTimes(1);
});
