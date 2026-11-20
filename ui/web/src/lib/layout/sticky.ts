// meiva:note
// The timeline's bottom zone: how close to the natural end a reader must be to follow the tail.
//
// The timeline's position has one owner, the view anchor
// (components/timeline/anchor/use-viewport-anchor.ts): the tail or a row at an offset. This
// module only sizes the zone in which a reader's own scroll reads back as "the tail". The width is
// pointer-dependent (the component picks the preset from `(pointer: coarse)`):
//   - touch: bottomPxRatio * clientHeight clamped to [80, 200]. The wide
//     zone absorbs iOS WebKit momentum + overscroll bounce, which leaves
//     scrollTop 100-150px short of the bottom even when the user clearly
//     means "at the bottom".
//   - mouse / trackpad: a much tighter zone (no overscroll bounce exists),
//     so a small deliberate scroll-up to read a message escapes the tail
//     instead of being yanked back on the next streamed item.

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

// Mouse / trackpad: tight zone. No overscroll bounce to absorb, so a
// deliberate scroll-up of one wheel notch leaves the bottom zone and
// unsticks — the timeline stops yanking the user back while they read.
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
