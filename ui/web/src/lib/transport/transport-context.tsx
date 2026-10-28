"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { Api } from "./gateway/api-factory";
import type { StreamHub } from "./stream/stream-hub";

const ApiContext = createContext<Api | null>(null);
const StreamHubContext = createContext<StreamHub | null>(null);
const WallClockContext = createContext<(() => number) | null>(null);

export function ApiProvider({ api, children }: { api: Api; children?: ReactNode }) {
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

// meiva:note
// The one way a component or hook reaches the gateway: the transport is a value the page
// hands down through <ApiProvider>, so a test renders the same provider with a fake and
// nothing replaces the transport module. There is no fallback to the app's own client:
// a missing provider is a composition error, and it says so.
export function useApi(): Api {
  const api = useContext(ApiContext);
  if (api === null) throw new Error("useApi must be used inside <ApiProvider>");
  return api;
}

export function StreamHubProvider({ hub, children }: { hub: StreamHub; children: ReactNode }) {
  return <StreamHubContext.Provider value={hub}>{children}</StreamHubContext.Provider>;
}

export function useStreamHub(): StreamHub {
  const hub = useContext(StreamHubContext);
  if (hub === null) throw new Error("useStreamHub must be used inside <StreamHubProvider>");
  return hub;
}

// meiva:note
// The wall clock the page reads, in epoch milliseconds: the root hands in the stream
// transport's `wallNow`, a test hands in the instant it wants.
export function WallClockProvider({ wallNow, children }: { wallNow: () => number; children: ReactNode }) {
  return <WallClockContext.Provider value={wallNow}>{children}</WallClockContext.Provider>;
}

export function useWallNow(): () => number {
  const wallNow = useContext(WallClockContext);
  if (wallNow === null) throw new Error("useWallNow must be used inside <WallClockProvider>");
  return wallNow;
}
