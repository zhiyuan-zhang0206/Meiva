"use client";

import { useEffect } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";
import { pinnedShellHeight } from "@/lib/browser/viewport";

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
// Renders nothing; mounted from app/layout.tsx inside <Providers> — the same file that declares the
// Android meta — so every route, login included, is covered.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
export function VisualViewportHeightSync() {
  const viewport = useBrowser().viewport;
  useEffect(() => {
    const rootStyle = document.documentElement.style;
    const clearPin = () => {
      rootStyle.height = "";
      rootStyle.removeProperty("--visual-viewport-height");
      rootStyle.removeProperty("--safe-area-bottom");
    };
    const sync = () => {
      const reading = viewport.read();
      const pinned = pinnedShellHeight(reading);
      if (pinned === null) {
        clearPin();
      } else {
        // meiva:note
        // CSS `vh`/`dvh` and `position: fixed` resolve against the layout viewport on WebKit,
        // ignoring `html.style.height`, and `env(safe-area-inset-bottom)` stays non-zero even
        // when the on-screen keyboard covers the Home Indicator. Publishing `--visual-viewport-height`
        // and zeroing `--safe-area-bottom` lets fixed overlays, dialogs and max-height caps shrink
        // with the visible band above the keyboard.
        rootStyle.height = `${pinned}px`;
        rootStyle.setProperty("--visual-viewport-height", `${pinned}px`);
        rootStyle.setProperty("--safe-area-bottom", "0px");
      }
      if (reading.visual !== null) {
        viewport.resetScroll();
      }
    };

    sync();
    // meiva:note
    // `resize` carries height changes (keyboard opens/closes, orientation or toolbar moves);
    // `scroll` catches WebKit's pan-the-focused-field pass and `WKWebView` scroll-inset shifts,
    // which can land after the resize.
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
