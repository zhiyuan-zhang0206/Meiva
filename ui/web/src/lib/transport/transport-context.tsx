"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { Api } from "./gateway/api-factory";
import type { StreamHub } from "./stream/stream-hub";

const ApiContext = createContext<Api | null>(null);
const StreamHubContext = createContext<StreamHub | null>(null);

export function ApiProvider({ api, children }: { api: Api; children?: ReactNode }) {
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function useApi(fallback?: Api): Api {
  const api = useContext(ApiContext);
  if (fallback !== undefined && typeof fallback.head !== "function") return fallback;
  if (api !== null) return api;
  if (fallback !== undefined) return fallback;
  throw new Error("useApi must be used inside <ApiProvider>");
}

export function StreamHubProvider({ hub, children }: { hub: StreamHub; children: ReactNode }) {
  return <StreamHubContext.Provider value={hub}>{children}</StreamHubContext.Provider>;
}

export function useStreamHub(): StreamHub {
  const hub = useContext(StreamHubContext);
  if (hub === null) throw new Error("useStreamHub must be used inside <StreamHubProvider>");
  return hub;
}
