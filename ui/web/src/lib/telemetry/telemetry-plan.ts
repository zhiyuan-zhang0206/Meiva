const TELEMETRY_ENDPOINT = "/api/frontend-telemetry";
const DEDUPE_MS = 2000;
const RATE_LIMIT = 100;
const RATE_WINDOW_MS = 60_000;
const BUFFER_CAP = 200;

export type TelemetryElement =
  | "api-timing"
  | "compact"
  | "composer-latency"
  | "composer-send"
  | "composer-stop"
  | "fork"
  | "nav-timing"
  | "page-view"
  | "restart"
  | "resurrect"
  | "setting-change"
  | "sse-transport"
  | "spawn"
  | "terminate"
  | "web-vitals";

export interface TrackOptions {
  page?: string;
  key?: string;
  value?: unknown;
  dedupe?: boolean;
}

export interface PendingEvent {
  page: string;
  element: string;
  key?: string;
  value?: string;
  ts: number;
}

export interface TelemetryBufferState {
  enabled: boolean;
  sessionId: string;
  currentPage: string;
  buffer: PendingEvent[];
  lastTracked: Map<string, number>;
  sentAt: number[];
}

export function createTelemetryBufferState(enabled: boolean): TelemetryBufferState {
  return {
    enabled,
    sessionId: "",
    currentPage: "unknown",
    buffer: [],
    lastTracked: new Map<string, number>(),
    sentAt: [],
  };
}

export function resetTelemetryBufferState(
  state: TelemetryBufferState,
  enabled: boolean,
): void {
  state.enabled = enabled;
  state.sessionId = "";
  state.currentPage = "unknown";
  state.buffer = [];
  state.lastTracked.clear();
  state.sentAt.length = 0;
}

export function sanitizeValue(value: unknown): string | undefined {
  if (typeof value === "boolean" || typeof value === "number") {
    return String(value);
  }
  if (typeof value === "string") {
    return value.length > 64 ? value.slice(0, 64) : value;
  }
  return undefined;
}

export function recordTrackEvent(
  state: TelemetryBufferState,
  element: TelemetryElement,
  opts: TrackOptions,
  nowMs: number,
): boolean {
  if (!state.enabled) return false;
  const page = opts.page ?? state.currentPage;
  const key = opts.key;
  if (opts.dedupe !== false) {
    const dedupeKey = `${page}|${element}|${key ?? ""}`;
    const last = state.lastTracked.get(dedupeKey);
    if (last !== undefined && nowMs - last < DEDUPE_MS) return false;
    state.lastTracked.set(dedupeKey, nowMs);
  }
  while (state.sentAt.length > 0 && nowMs - state.sentAt[0] > RATE_WINDOW_MS) {
    state.sentAt.shift();
  }
  if (state.sentAt.length + state.buffer.length >= RATE_LIMIT) return false;
  const value = sanitizeValue(opts.value);
  const ev: PendingEvent = { page, element, ts: nowMs };
  if (key !== undefined) ev.key = key;
  if (value !== undefined) ev.value = value;
  if (state.buffer.length >= BUFFER_CAP) state.buffer.shift();
  state.buffer.push(ev);
  return true;
}

export function takeFlushBatch(
  state: TelemetryBufferState,
  sessionId: string,
  nowMs: number,
  apiBase: string,
): { url: string; body: string } | null {
  if (!state.enabled || state.buffer.length === 0) return null;
  const events = state.buffer;
  state.buffer = [];
  for (const _event of events) {
    state.sentAt.push(nowMs);
  }
  return {
    url: `${apiBase}${TELEMETRY_ENDPOINT}`,
    body: JSON.stringify({ session_id: sessionId, events }),
  };
}

export function normalizePage(pathname: string): string {
  const p = pathname.replace(/^\/+/, "").replace(/\/+$/, "");
  if (p === "") return "home";
  const [head, second] = p.split("/");
  if (head === "shell" && /^\d+$/.test(second)) {
    return "shell";
  }
  return p;
}
