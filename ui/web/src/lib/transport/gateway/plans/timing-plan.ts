const SLOW_REQUEST_MS = 800;
const TELEMETRY_PATH = "/api/frontend-telemetry";

export const INSPECT_REQUEST_TIMEOUT_MS = 35_000;
export const STATS_REQUEST_TIMEOUT_MS = 40_000;

export interface SlowRequestReport {
  key: string;
  value: number;
  dedupe: false;
}

function apiTimingKey(path: string): string {
  const pathname = path.split("?", 1)[0].replace(/^\/api\/?/, "");

  return pathname
    .split("/")
    .map((segment) => (/^\d+$/.test(segment) ? "id" : segment))
    .join(".");
}

export function slowRequestReport(path: string, durationMs: number): SlowRequestReport | null {
  if (durationMs <= SLOW_REQUEST_MS || path.split("?", 1)[0] === TELEMETRY_PATH) return null;
  return { key: apiTimingKey(path), value: Math.round(durationMs), dedupe: false };
}
