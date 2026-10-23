// API base resolution runs once per module graph (once on the server and once
// in the browser). Keeping it outside api.ts lets telemetry use the same base
// without creating an api.ts ↔ telemetry.ts import cycle.
import { gatewayOriginForRequest } from "./gateway-origin";

export type TransportTelemetryElement = "api-timing" | "sse-transport";

export interface TransportTrackOptions {
  page?: string;
  key?: string;
  value?: unknown;
  dedupe?: boolean;
}

type TransportTracker = (
  element: TransportTelemetryElement,
  opts?: TransportTrackOptions,
) => void;

const telemetrySink: { track: TransportTracker | null } = { track: null };

export function registerTransportTelemetry(tracker: TransportTracker | null): void {
  telemetrySink.track = tracker;
}

export function trackTransport(
  element: TransportTelemetryElement,
  opts: TransportTrackOptions = {},
): void {
  telemetrySink.track?.(element, opts);
}

export const API_BASE = ((): string => {
  if (process.env.NEXT_PUBLIC_API_BASE) return process.env.NEXT_PUBLIC_API_BASE;
  if (typeof window === "undefined") return "";
  return gatewayOriginForRequest(new URL(window.location.href), {
    browserOrigin: process.env.NEXT_PUBLIC_BROWSER_ORIGIN,
    gatewayPort: process.env.NEXT_PUBLIC_GATEWAY_PORT,
  });
})();
