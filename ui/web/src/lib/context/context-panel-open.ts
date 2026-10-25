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

export interface ContextPanelStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

const panelStore = {
  listeners: new Set<() => void>(),
  storage: undefined as ContextPanelStorage | undefined,
  fallbackOpen: undefined as boolean | undefined,
};

function resolveStorage(storage?: ContextPanelStorage): ContextPanelStorage {
  return storage ?? panelStore.storage ?? localStorage;
}

export function readContextPanelOpen(storage?: ContextPanelStorage): boolean {
  try {
    const raw = resolveStorage(storage).getItem(CONTEXT_PANEL_KEY);
    panelStore.fallbackOpen = undefined;
    return raw !== "0";
  } catch {
    return panelStore.fallbackOpen ?? true;
  }
}

function writeContextPanelOpen(open: boolean, storage?: ContextPanelStorage): void {
  panelStore.fallbackOpen = open;
  try {
    resolveStorage(storage).setItem(CONTEXT_PANEL_KEY, open ? "1" : "0");
  } catch {
    return;
  }
}

function subscribe(listener: () => void): () => void {
  panelStore.listeners.add(listener);
  return () => panelStore.listeners.delete(listener);
}

export function setContextPanelStorageForTest(storage?: ContextPanelStorage): void {
  panelStore.storage = storage;
  panelStore.fallbackOpen = undefined;
}

export function useContextPanelOpen(): [open: boolean, setOpen: (open: boolean) => void] {
  const open = useSyncExternalStore(subscribe, () => readContextPanelOpen(), () => true);
  const setOpen = useCallback((next: boolean) => {
    writeContextPanelOpen(next);
    for (const listener of panelStore.listeners) listener();
  }, []);
  return [open, setOpen];
}
