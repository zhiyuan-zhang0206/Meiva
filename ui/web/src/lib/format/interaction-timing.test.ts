import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  telemetryTestControl,
  type TelemetryElement,
  type TrackOptions,
} from "../telemetry/telemetry";
import {
  clearMessageSent,
  interactionTimingResetForTest,
  markMessageSent,
  noteTurnStart,
} from "./interaction-timing";

const track = vi.fn<(element: TelemetryElement, opts?: TrackOptions) => void>();

beforeEach(() => {
  interactionTimingResetForTest();
  track.mockReset();
  telemetryTestControl.intercept({ onTrack: track });
});

afterEach(() => {
  interactionTimingResetForTest();
  telemetryTestControl.reset();
  vi.restoreAllMocks();
});

describe("composer send-to-turn-start timing", () => {
  it("reports a positive bounded delta once", () => {
    vi.spyOn(performance, "now")
      .mockReturnValueOnce(1_000)
      .mockReturnValueOnce(1_123.6)
      .mockReturnValueOnce(1_500);

    markMessageSent(42);
    noteTurnStart(42);
    noteTurnStart(42);

    expect(track).toHaveBeenCalledTimes(1);
    expect(track).toHaveBeenCalledWith("composer-latency", {
      key: "send-to-turn-start",
      value: 124,
    });
  });

  it("does nothing without a pending send mark", () => {
    noteTurnStart(42);

    expect(track).not.toHaveBeenCalled();
  });

  it("drops a delta beyond the 120 second sanity bound", () => {
    vi.spyOn(performance, "now")
      .mockReturnValueOnce(1_000)
      .mockReturnValueOnce(121_001);

    markMessageSent(42);
    noteTurnStart(42);

    expect(track).not.toHaveBeenCalled();
  });

  it("clears a failed or abandoned send mark", () => {
    vi.spyOn(performance, "now")
      .mockReturnValueOnce(1_000)
      .mockReturnValueOnce(1_200);

    markMessageSent(42);
    clearMessageSent(42);
    noteTurnStart(42);

    expect(track).not.toHaveBeenCalled();
  });
});
