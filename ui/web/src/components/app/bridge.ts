"use client";

import { useSyncExternalStore } from "react";

export interface AppUpdatePrompt {
  readonly status: "available" | "installing" | "download-fallback";
  readonly versionName: string;
  readonly versionCode: number;
  readonly text: string;
  readonly actionLabel: string;
  readonly downloadPage: string | null;
  readonly install: () => void;
  readonly dismiss: () => void;
}

export interface HostShellPayload {
  readonly version?: string;
  readonly platform?: string;
  readonly entryUrl?: string | null;
  readonly gatewayUrl?: string | null;
  readonly autoLogin?: boolean;
  readonly notifications?: boolean;
  readonly invoke?: (command: string, args?: unknown) => Promise<unknown>;
  readonly update?: AppUpdatePrompt | null;
}

// meiva:note
// The native host shell (`ui/app` on macOS and Android) publishes `window.__MEIVA_APP__`
// before the console boots and refreshes it on page load / update events. The web
// console reads only this typed bridge contract — never raw Tauri internals.
// meiva:link ui/app/docs/app.meiva.okf.md
declare global {
  interface Window {
    readonly __MEIVA_APP__?: HostShellPayload;
  }
}

export interface AppBridge {
  readonly openSettings: () => Promise<unknown>;
  readonly update: AppUpdatePrompt | null;
}

let lastInvoke: ((command: string, args?: unknown) => Promise<unknown>) | null = null;
let lastUpdate: AppUpdatePrompt | null = null;
let lastBridge: AppBridge | null = null;

export function readAppBridge(): AppBridge | null {
  if (typeof window === "undefined") return null;
  const host = window.__MEIVA_APP__;
  if (!host || typeof host.invoke !== "function") {
    lastInvoke = null;
    lastUpdate = null;
    lastBridge = null;
    return null;
  }
  const invoke = host.invoke;
  const update = host.update ?? null;
  if (invoke !== lastInvoke || update !== lastUpdate || lastBridge === null) {
    lastInvoke = invoke;
    lastUpdate = update;
    lastBridge = {
      openSettings: () => invoke("app_open_settings"),
      update,
    };
  }
  return lastBridge;
}

function subscribeAppBridge(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener("meiva-app-config", onStoreChange);
  window.addEventListener("meiva-app-update", onStoreChange);
  return () => {
    window.removeEventListener("meiva-app-config", onStoreChange);
    window.removeEventListener("meiva-app-update", onStoreChange);
  };
}

function serverSnapshot(): AppBridge | null {
  return null;
}

export function useAppBridge(): AppBridge | null {
  return useSyncExternalStore(subscribeAppBridge, readAppBridge, serverSnapshot);
}
