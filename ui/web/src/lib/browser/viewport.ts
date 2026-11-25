// meiva:note
// The on-screen keyboard contract's decision, as a value: a phone keyboard shrinks only the VISUAL
// viewport while the shell keeps its LAYOUT height, so the composer ends up under the keyboard and
// Safari pans the page to reveal the focused field. The pin is the visual height, and only while
// the shortfall is big enough to be a keyboard rather than browser chrome, and only at scale 1 —
// pinning a viewport the reader is already pinch-zooming fights the zoom. "Scale 1" tolerates
// `VIEWPORT_SCALE_EPSILON` of WebKit float jitter (`0.99999`); a real pinch (1.25, 2) stays outside it.
// meiva:link ui/web/src/docs/frontend-state/browser-port.md

import type { ViewportReading } from "./browser";

// meiva:note
// The shortfall that reads as an on-screen keyboard rather than browser chrome: iOS Safari's
// toolbar collapse/expand moves the visual height by tens of pixels, while even the smallest phone
// keyboards are ~200px. Below it the shell keeps its layout-viewport height, so toolbar motion
// alone never shrinks the page.
export const KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX = 150;

// meiva:note
// The width mismatch that indicates a stuck layout viewport after a rotation on iPadOS CriOS /
// WKWebView, rather than a desktop vertical scrollbar (~15-17px).
export const VIEWPORT_WIDTH_PIN_MIN_DELTA_PX = 24;

export const VIEWPORT_SCALE_EPSILON = 0.02;

export function isUnzoomed(scale: number): boolean {
  return Math.abs(scale - 1) <= VIEWPORT_SCALE_EPSILON;
}

// meiva:note
// The height the shell is pinned to while the keyboard is up, or null to keep the layout
// viewport's. Rounded: a fractional visual height would leak half pixels into the shell height.
// A visual band TALLER than the layout viewport is the same stuck-layout state as the width case
// below (a landscape layout viewport left behind by a portrait rotation), so both directions pin.
export function pinnedShellHeight(
  reading: ViewportReading,
  minShortfall: number = KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX,
): number | null {
  const visual = reading.visual;
  if (visual === null) return null;
  if (!isUnzoomed(visual.scale)) return null;
  if (Math.abs(reading.layoutHeight - visual.height) <= minShortfall) return null;
  return Math.round(visual.height);
}

// meiva:note
// The width the shell is pinned to when an unzoomed visual viewport differs from the layout
// viewport by more than a scrollbar: iPadOS CriOS leaves the tab's layout viewport width stuck at
// the OLD orientation's dimension after a rotation — narrower visual than layout after
// landscape-to-portrait, wider after portrait-to-landscape — even across reloads. Null when the
// layout viewport already matches.
export function pinnedShellWidth(
  reading: ViewportReading,
  minShortfall: number = VIEWPORT_WIDTH_PIN_MIN_DELTA_PX,
): number | null {
  const visual = reading.visual;
  if (visual === null) return null;
  if (!isUnzoomed(visual.scale)) return null;
  const layoutWidth = reading.layoutWidth;
  const visualWidth = visual.width;
  if (layoutWidth === undefined || visualWidth === undefined || visualWidth <= 0) return null;
  if (Math.abs(layoutWidth - visualWidth) <= minShortfall) return null;
  return Math.round(visualWidth);
}
