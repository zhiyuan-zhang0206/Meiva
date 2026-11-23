// meiva:note
// The timeline's down arrow hides inside a pointer-dependent zone near the natural end.
// The viewport only resumes following once the reader reaches the natural end itself.

export interface StickyThresholds {
  // "At the bottom" zone = bottomPxRatio * clientHeight, clamped to
  // [bottomPxMin, bottomPxMax].
  bottomPxRatio: number;
  bottomPxMin: number;
  bottomPxMax: number;
}

// Touch devices: wide bounce-tolerant zone.
export const TOUCH_STICKY_THRESHOLDS: StickyThresholds = {
  bottomPxRatio: 0.2,
  bottomPxMin: 80,
  bottomPxMax: 200,
};

// Mouse and trackpad: a tighter visibility zone for wheel and trackpad scrolling.
export const POINTER_STICKY_THRESHOLDS: StickyThresholds = {
  bottomPxRatio: 0.05,
  bottomPxMin: 24,
  bottomPxMax: 72,
};

// Default = touch preset, preserving prior behavior for callers that do
// not pass thresholds.
const DEFAULT_STICKY_THRESHOLDS = TOUCH_STICKY_THRESHOLDS;

// meiva:note
// Effective "at the bottom" zone in px: bottomPxRatio * clientHeight,
// clamped to [bottomPxMin, bottomPxMax].
export function bottomZone(
  clientHeight: number,
  thresholds: StickyThresholds = DEFAULT_STICKY_THRESHOLDS,
): number {
  return Math.min(
    thresholds.bottomPxMax,
    Math.max(thresholds.bottomPxMin, clientHeight * thresholds.bottomPxRatio),
  );
}
