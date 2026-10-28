"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { Browser } from "./browser";

const BrowserContext = createContext<Browser | null>(null);

export function BrowserProvider({ browser, children }: { browser: Browser; children?: ReactNode }) {
  return <BrowserContext.Provider value={browser}>{children}</BrowserContext.Provider>;
}

// meiva:note
// The one way a component reaches the page: the browser is a value the root hands down through
// <BrowserProvider>, so a test renders the same provider with `fakeBrowser()` and nothing stubs a
// global. There is no fallback to the real window — a missing provider is a composition error and
// says so. That means a test that renders a component bare (without the provider) throws on
// purpose: the throw is the design, not a regression, and the fix is to mount the provider with a
// fake, not to add a default.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
export function useBrowser(): Browser {
  const browser = useContext(BrowserContext);
  if (browser === null) throw new Error("useBrowser must be used inside <BrowserProvider>");
  return browser;
}
