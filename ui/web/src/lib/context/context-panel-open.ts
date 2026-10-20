// meiva:note
// Open/closed is a view state of this browser, like the column widths beside it
// (lib/layout/panel-layout-storage.ts), not a preference to sync across devices:
// the panel carries the active agent's shared context and the header toggle
// reopens it. It used to be a user_settings row (`display.context_panel_open`),
// which made collapsing a panel look like a cross-agent save (user 2026-10-20).
// Read through useSyncExternalStore so the server snapshot is the default and
// hydration never disagrees with the first paint; blocked storage reads open and
// blocked writes keep the choice for this page view only.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { useCallback, useSyncExternalStore } from "react";

const CONTEXT_PANEL_KEY = "meiva.home.contextPanelOpen";
const listeners = new Set<() => void>();

export function readContextPanelOpen(): boolean {
  try {
    return localStorage.getItem(CONTEXT_PANEL_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeContextPanelOpen(open: boolean): void {
  try {
    localStorage.setItem(CONTEXT_PANEL_KEY, open ? "1" : "0");
  } catch {
    return;
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useContextPanelOpen(): [open: boolean, setOpen: (open: boolean) => void] {
  const open = useSyncExternalStore(subscribe, readContextPanelOpen, () => true);
  const setOpen = useCallback((next: boolean) => {
    writeContextPanelOpen(next);
    for (const listener of listeners) listener();
  }, []);
  return [open, setOpen];
}
