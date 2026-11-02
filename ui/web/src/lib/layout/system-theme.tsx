"use client";

// meiva:note
// next-themes, as the theme service the app mounts, and `app/layout.tsx` is the only caller. Its
// inline script toggles .dark on <html> before hydrate (no FOUC) and a matchMedia listener follows
// system settings; the provider carries the request nonce for that script.
//
// attribute="class"      → use class, not data-attribute (matches shadcn)
// defaultTheme="system"  → follow system by default
// enableSystem           → allow "system" as a valid theme value
// disableTransitionOnChange → briefly suspend CSS transitions during a
//   theme swap to avoid color-transition flicker mid-swap
import { ThemeProvider, useTheme } from "next-themes";

import type { ThemeRuntime } from "./theme-runtime";

export const SYSTEM_THEME: ThemeRuntime = {
  Provider: ({ nonce, children }) => (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      nonce={nonce}
    >
      {children}
    </ThemeProvider>
  ),
  useResolvedTheme: () => useTheme().resolvedTheme,
};
