// API base resolution runs once per module graph (once on the server and once
// in the browser). Keeping it outside api.ts lets telemetry use the same base
// without creating an api.ts ↔ telemetry.ts import cycle.
import { gatewayBaseForPage } from "./gateway-origin";

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

export const API_BASE = gatewayBaseForPage(
  {
    apiBase: process.env.NEXT_PUBLIC_API_BASE,
    browserOrigin: process.env.NEXT_PUBLIC_BROWSER_ORIGIN,
    gatewayPort: process.env.NEXT_PUBLIC_GATEWAY_PORT,
  },
  typeof window === "undefined" ? null : new URL(window.location.href),
);
