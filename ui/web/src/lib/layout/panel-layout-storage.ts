"use client";

// meiva:note
// Storage for react-resizable-panels `useDefaultLayout`, over the browser port's local storage.
// A group's layout is one map keyed by panel id under `react-resizable-panels:<id>`
// (`{ "panel-sidebar": 30, "panel-main": 70 }`). The port answers a read with one of three
// states: `absent` (this browser never saved a layout) and `unavailable` (a frame with no
// storage — a server render, or a browser that refuses site data) both leave the group on its
// declared defaults, which is the frame the library renders with nothing stored at all; the
// distinction still matters on the write side, where the port reports whether the layout was
// kept.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { useMemo } from "react";
import type { LayoutStorage } from "react-resizable-panels";

import type { BrowserStorage } from "@/lib/browser/browser";
import { useBrowser } from "@/lib/browser/browser-provider";

export function panelLayoutStorage(storage: BrowserStorage): LayoutStorage {
  return {
    getItem(key) {
      const stored = storage.read(key);
      switch (stored.state) {
        case "found":
          return stored.value;
        case "absent":
          return null;
        case "unavailable":
          return null;
      }
    },
    setItem(key, value) {
      storage.write(key, value);
    },
  };
}

export function usePanelLayoutStorage(): LayoutStorage {
  const browser = useBrowser();
  return useMemo(() => panelLayoutStorage(browser.local), [browser]);
}
