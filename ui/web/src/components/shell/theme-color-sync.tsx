"use client";

import { useEffect } from "react";

// meiva:note
// The palette owns the browser-chrome colour (`--theme-color` in globals.css,
// one per scheme). This mirrors it into <meta name="theme-color"> so Android
// Chrome tints the status bar and navigation bar like the page — including when
// the in-app theme overrides the OS preference, which the SSR media queries
// cannot see.
// meiva:link ui/web/src/docs/frontend.meiva.okf.md
// meiva:note
// `resolvedTheme` is the scheme the page actually shows (`"system"` already resolved by the theme
// provider), and it is a value here rather than a hook: the effect re-runs when the scheme changes,
// and the caller that already reads the theme provider passes it down.
export function ThemeColorSync({ resolvedTheme }: { resolvedTheme: string | undefined }) {
  useEffect(() => {
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue("--theme-color")
      .trim();
    if (!color) return;
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.setAttribute("content", color);
    }
  }, [resolvedTheme]);
  return null;
}
