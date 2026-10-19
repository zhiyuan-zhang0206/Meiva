"use client";

import { useTheme } from "next-themes";
import { useEffect } from "react";

// meiva:note
// The palette owns the browser-chrome colour (`--theme-color` in globals.css,
// one per scheme). This mirrors it into <meta name="theme-color"> so Android
// Chrome tints the status bar and navigation bar like the page — including when
// the in-app theme overrides the OS preference, which the SSR media queries
// cannot see.
// meiva:link ui/web/src/docs/frontend.meiva.okf.md
export function ThemeColorSync() {
  const { resolvedTheme } = useTheme();
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
