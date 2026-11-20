// meiva:note
// The arrow's ride to the tail is timed here rather than by the browser's smooth scroll, whose
// duration grows with the distance without bound (user report 2026-11-20: "sometimes it reaches the
// bottom very slowly"). The ride's length grows linearly with the distance, RIDE_PX_PER_MS, and never
// exceeds RIDE_MAX_MS: whatever the distance, the reader is at the bottom within a second (user
// ruling 2026-11-20). RIDE_MIN_MS keeps a short hop visible as motion rather than a jump.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
export const RIDE_MAX_MS = 1000;
const RIDE_MIN_MS = 120;
const RIDE_PX_PER_MS = 4;

export function rideDuration(distance: number): number {
  return Math.min(RIDE_MAX_MS, Math.max(RIDE_MIN_MS, Math.abs(distance) / RIDE_PX_PER_MS));
}

// meiva:note
// Ease-out: the ride leaves fast and settles onto the end.
export function rideProgress(elapsed: number, duration: number): number {
  const t = Math.min(1, Math.max(0, elapsed / duration));
  return 1 - (1 - t) ** 3;
}

// meiva:note
// The ride's frames and clock. A test's layout model replaces them to step a ride deterministically.
export const rideClock = {
  frame: (callback: () => void): number => requestAnimationFrame(callback),
  cancel: (id: number): void => cancelAnimationFrame(id),
  now: (): number => performance.now(),
};
