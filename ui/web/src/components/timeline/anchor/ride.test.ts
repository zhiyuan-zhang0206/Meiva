import { describe, expect, it } from "vitest";

import { RIDE_MAX_MS, rideDuration, rideProgress } from "./ride";

// meiva:note
// The arrow reaches the bottom within a second however far the reader scrolled up (user ruling
// 2026-11-20): the ride's length grows with the distance until it is capped.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
describe("the arrow's ride", () => {
  it("grows linearly with the distance, then caps at one second", () => {
    expect(rideDuration(2000)).toBe(500);
    expect(rideDuration(3000)).toBe(750);
    expect(rideDuration(4000)).toBe(RIDE_MAX_MS);
    expect(rideDuration(250_000)).toBe(RIDE_MAX_MS);
    expect(RIDE_MAX_MS).toBe(1000);
  });

  it("keeps a short hop visible as motion", () => {
    expect(rideDuration(10)).toBe(120);
    expect(rideDuration(-2000)).toBe(500);
  });

  it("starts at the reader's position, eases out, and lands exactly at the end", () => {
    expect(rideProgress(0, 500)).toBe(0);
    expect(rideProgress(250, 500)).toBeGreaterThan(0.5);
    expect(rideProgress(500, 500)).toBe(1);
    expect(rideProgress(900, 500)).toBe(1);
  });
});
