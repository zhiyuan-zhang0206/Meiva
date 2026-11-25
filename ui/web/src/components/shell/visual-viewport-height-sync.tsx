"use client";

import { useEffect } from "react";

import type { ViewportCause } from "@/lib/browser/browser";
import { useBrowser } from "@/lib/browser/browser-provider";
import { isUnzoomed, pinnedShellHeight, pinnedShellWidth } from "@/lib/browser/viewport";

// meiva:note
// Keeps the app shell in step with the visual viewport while the on-screen keyboard is up (task
// #4779). The shell takes its height from the LAYOUT viewport (`h-full` on html/body = 100% of the
// initial containing block) and the composer sits in normal flow at the bottom of that column; a
// phone or tablet keyboard shrinks only the VISUAL viewport, so the composer keeps its place at
// the layout bottom — under the keyboard — and WebKit (`Safari` and `CriOS` `WKWebView`) pans the
// page to reveal the focused field. Pinning <html> to the visual height while the keyboard is up
// lets the existing flex chain shrink by itself, and resetting the root scroll (`resetScroll`)
// cancels WebKit's focus pan and `WKWebView` keyboard-inset scroll so the composer cannot be
// double-shifted to the top edge or leave a blank scrollable tail after the keyboard closes.
//
// Android Chrome never needs this effect: the viewport export in app/layout.tsx declares
// `interactive-widget=resizes-content`, so its keyboard shrinks the layout viewport natively and
// the delta read here stays ~0.
//
// The viewport is the browser port's (`useBrowser().viewport`), so a test states the heights a
// keyboard, a toolbar or a pinch produces instead of installing a stub on the global;
// `pinnedShellHeight` in lib/browser/viewport.ts owns the decision and this shell only applies it
// to the document.
//
// Rotation: iPadOS `WKWebView` (`CriOS`) can keep the previous orientation's layout viewport and
// zoom the page to fit it after a turn, in either direction, so the port tells this shell when the
// device `rotated` and the shell asks it to `resetScale` (a transient `minimum-scale=1,
// maximum-scale=1` on the viewport meta). While the layout width still disagrees with the visual
// width at scale 1, `pinnedShellWidth` pins <html> to the visual width as the fallback. A rotation
// is the one moment the scale is reset without asking the reading (the event, not a scale value,
// tells WebKit's zoom-to-fit from the reader's pinch); the `rotation-recheck` 180ms later resets
// again while the width is still stuck or the scale is still not 1 (WebKit ignored the first
// reset); a `recheck` (tab back in the foreground in the same orientation, mount) resets only
// while the width is still stuck, and a pinch is never pulled back.
//
// Renders nothing; mounted from app/layout.tsx inside <Providers> — the same file that declares the
// Android meta — so every route, login included, is covered.
// meiva:link ui/web/src/docs/frontend-state/browser-port.md
export function VisualViewportHeightSync() {
  const viewport = useBrowser().viewport;
  useEffect(() => {
    const rootStyle = document.documentElement.style;
    const clearHeightPin = () => {
      rootStyle.height = "";
      rootStyle.removeProperty("--visual-viewport-height");
      rootStyle.removeProperty("--safe-area-bottom");
    };
    const clearWidthPin = () => {
      rootStyle.width = "";
      rootStyle.minWidth = "";
      rootStyle.maxWidth = "";
      rootStyle.overflowX = "";
      rootStyle.removeProperty("--visual-viewport-width");
    };
    const clearPin = () => {
      clearHeightPin();
      clearWidthPin();
    };
    const sync = (cause: ViewportCause) => {
      const reading = viewport.read();
      const pinnedWidth = pinnedShellWidth(reading);
      const stuckScale = reading.visual !== null && !isUnzoomed(reading.visual.scale);
      if (
        cause === "rotated" ||
        (cause === "rotation-recheck" && (pinnedWidth !== null || stuckScale)) ||
        (cause === "recheck" && pinnedWidth !== null)
      ) {
        viewport.resetScale();
      }
      const pinned = pinnedShellHeight(reading);
      if (pinned === null) {
        clearHeightPin();
      } else {
        // meiva:note
        // CSS `vh`/`dvh` and `position: fixed` resolve against the layout viewport on WebKit,
        // ignoring `html.style.height`, and `env(safe-area-inset-bottom)` stays non-zero even
        // when the on-screen keyboard covers the Home Indicator. Publishing `--visual-viewport-height`
        // and zeroing `--safe-area-bottom` lets fixed overlays, dialogs and max-height caps shrink
        // with the visible band above the keyboard. A height mismatch that comes with a width
        // mismatch is a stuck rotation, not a keyboard: the Home Indicator is still there, so the
        // safe area stays.
        rootStyle.height = `${pinned}px`;
        rootStyle.setProperty("--visual-viewport-height", `${pinned}px`);
        if (pinnedWidth === null) {
          rootStyle.setProperty("--safe-area-bottom", "0px");
        } else {
          rootStyle.removeProperty("--safe-area-bottom");
        }
      }
      if (pinnedWidth === null) {
        clearWidthPin();
      } else {
        rootStyle.width = `${pinnedWidth}px`;
        rootStyle.minWidth = `${pinnedWidth}px`;
        rootStyle.maxWidth = `${pinnedWidth}px`;
        rootStyle.overflowX = "hidden";
        rootStyle.setProperty("--visual-viewport-width", `${pinnedWidth}px`);
      }
      if (reading.visual !== null) {
        viewport.resetScroll();
      }
    };

    sync("recheck");
    // meiva:note
    // `resize` carries height changes (keyboard opens/closes, toolbar moves); `scroll` catches
    // WebKit's pan-the-focused-field pass and `WKWebView` scroll-inset shifts, which can land after
    // the resize; rotations and the tab returning to the foreground arrive as their own causes.
    const unsubscribe = viewport.subscribe(sync);
    return () => {
      unsubscribe();
      // meiva:note
      // Unmount must not leave the shell stuck at a keyboard height.
      clearPin();
    };
  }, [viewport]);

  return null;
}
