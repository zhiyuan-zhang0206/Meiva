// meiva:note
// The column cap is a pixel ceiling (`<px>px`), not a viewport ratio (`vw`): a
// `vw` cap shrank the conversation column against the whole browser window even
// when two panes sat side-by-side in the same window, preventing the messages
// column from filling half the screen (user 2026-10-30). With a pixel max-width
// on a `w-full` column, the timeline fills its pane up to the chosen pixel cap
// and only adds centered gutters once the pane itself is wider.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
export const TIMELINE_WIDTH_PX_DEFAULT = 768;
export const TIMELINE_WIDTH_PX_MIN = 480;
export const TIMELINE_WIDTH_PX_MAX = 1280;
export const TIMELINE_WIDTH_PX_STEP = 32;

export const TIMELINE_NARROW_BREAKPOINT_PX = 768;

export function clampTimelineWidthPx(value: unknown): number {
  const n =
    typeof value === "number" && Number.isFinite(value)
      ? Math.round(value)
      : TIMELINE_WIDTH_PX_DEFAULT;
  return Math.min(TIMELINE_WIDTH_PX_MAX, Math.max(TIMELINE_WIDTH_PX_MIN, n));
}

export function timelineMaxWidthCss(widthPx: unknown): string {
  return `${clampTimelineWidthPx(widthPx)}px`;
}
