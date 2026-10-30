// meiva:note
// The on-screen keyboard contract's decision, as a value: a phone keyboard shrinks only the VISUAL
// viewport while the shell keeps its LAYOUT height, so the composer ends up under the keyboard and
// Safari pans the page to reveal the focused field. The pin is the visual height, and only while
// the shortfall is big enough to be a keyboard rather than browser chrome, and only at scale 1 —
// pinning a viewport the reader is already pinch-zooming fights the zoom.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import type { ViewportReading } from "./browser";

// meiva:note
// The shortfall that reads as an on-screen keyboard rather than browser chrome: iOS Safari's
// toolbar collapse/expand moves the visual height by tens of pixels, while even the smallest phone
// keyboards are ~200px. Below it the shell keeps its layout-viewport height, so toolbar motion
// alone never shrinks the page.
export const KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX = 150;

// meiva:note
// The height the shell is pinned to while the keyboard is up, or null to keep the layout
// viewport's. Rounded: a fractional visual height would leak half pixels into the shell height.
export function pinnedShellHeight(
  reading: ViewportReading,
  minShortfall: number = KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX,
): number | null {
  const visual = reading.visual;
  if (visual === null) return null;
  if (visual.scale !== 1) return null;
  if (reading.layoutHeight - visual.height <= minShortfall) return null;
  return Math.round(visual.height);
}
