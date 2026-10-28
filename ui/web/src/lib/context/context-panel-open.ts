// meiva:note
// Open/closed is a view state of this browser, like the column widths beside it
// (lib/layout/panel-layout-storage.ts), not a preference to sync across devices:
// the panel carries the active agent's shared context and the header toggle
// reopens it. It used to be a user_settings row (`display.context_panel_open`),
// which made collapsing a panel look like a cross-agent save (user 2026-10-20).
// Read through useSyncExternalStore so the server snapshot is the default and
// hydration never disagrees with the first paint.
//
// The value lives in the browser port's local storage, whose read answers which
// of three things happened: a stored value is the truth; `absent` (nobody chose
// on this browser yet) opens the panel, which is its product default; and
// `unavailable` (a frame with no storage — a server render, or a browser that
// refuses site data) leaves the page view's own choice standing in, so toggling
// still works for as long as the tab lives.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { useCallback, useSyncExternalStore } from "react";

import type { BrowserStorage } from "@/lib/browser/browser";
import { useBrowser } from "@/lib/browser/browser-provider";

const CONTEXT_PANEL_KEY = "meiva.home.contextPanelOpen";

const pageView: { choice: boolean | undefined } = { choice: undefined };

const listeners = new Set<() => void>();

export function readContextPanelOpen(storage: BrowserStorage): boolean {
  const stored = storage.read(CONTEXT_PANEL_KEY);
  switch (stored.state) {
    case "found":
      return stored.value !== "0";
    case "absent":
      return pageView.choice ?? true;
    case "unavailable":
      return pageView.choice ?? true;
  }
}

export function writeContextPanelOpen(storage: BrowserStorage, open: boolean): void {
  pageView.choice = open;
  storage.write(CONTEXT_PANEL_KEY, open ? "1" : "0");
  for (const listener of listeners) listener();
}

export function useContextPanelOpen(): [open: boolean, setOpen: (open: boolean) => void] {
  const browser = useBrowser();
  const storage = browser.local;
  const open = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => readContextPanelOpen(storage),
    () => true,
  );
  const setOpen = useCallback(
    (next: boolean) => {
      writeContextPanelOpen(storage, next);
    },
    [storage],
  );
  return [open, setOpen];
}
