"use client";

import { useEffect } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";
import { pinnedShellHeight } from "@/lib/browser/viewport";

// meiva:note
// Keeps the app shell in step with the visual viewport while the on-screen keyboard is up (task
// #4779). The shell takes its height from the LAYOUT viewport (`h-full` on html/body = 100% of the
// initial containing block) and the composer sits in normal flow at the bottom of that column; a
// phone keyboard shrinks only the VISUAL viewport, so the composer keeps its place at the layout
// bottom — under the keyboard — and Safari pans the page to reveal the focused field. Pinning
// <html> to the visual height while the keyboard is up lets the existing flex chain shrink by
// itself: the composer lands inside the visible band and Safari has nothing to pan for.
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
    const sync = () => {
      const pinned = pinnedShellHeight(viewport.read());
      document.documentElement.style.height = pinned === null ? "" : `${pinned}px`;
    };

    sync();
    // meiva:note
    // `resize` carries height changes (keyboard opens/closes, toolbar moves); `scroll` catches
    // Safari's pan-the-focused-field pass, which can land after the resize.
    const unsubscribe = viewport.subscribe(sync);
    return () => {
      unsubscribe();
      // meiva:note
      // Unmount must not leave the shell stuck at a keyboard height.
      document.documentElement.style.height = "";
    };
  }, [viewport]);

  return null;
}
