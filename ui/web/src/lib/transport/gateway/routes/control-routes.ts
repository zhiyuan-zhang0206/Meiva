import type { AlertClassRow, AlertsWindow } from "../../../contracts/types";
import { withQuery } from "./route-query";

export function questionsPath(params?: { limit?: number }): string {
  const sp = new URLSearchParams();
  if (params?.limit != null) sp.set("limit", String(params.limit));
  return withQuery("/api/questions", sp);
}

export function fleetGraphPath(opts?: { hours?: number; decayLambda?: number }): string {
  const params = new URLSearchParams();
  if (opts?.hours != null) params.set("hours", String(opts.hours));
  if (opts?.decayLambda != null) params.set("decay_lambda", String(opts.decayLambda));
  return withQuery("/api/fleet/graph", params);
}

export function memoryNotePath(path: string): string {
  return `/api/memory/note?${new URLSearchParams({ path }).toString()}`;
}

export function alertClassSamplesPath(
  cls: Pick<AlertClassRow, "level" | "event_name" | "source" | "process">,
  hours: number,
): string {
  const q = new URLSearchParams({
    level: cls.level,
    event_name: cls.event_name,
    source: cls.source,
    process: cls.process,
    hours: String(hours),
  });
  return `/api/stats/alert-classes/samples?${q.toString()}`;
}

export function alertsPath(params: {
  window?: AlertsWindow;
  status?: string;
  severity?: string;
  limit?: number;
}): string {
  const q = new URLSearchParams();
  if (params.window) q.set("window", params.window);
  if (params.status) q.set("status", params.status);
  if (params.severity) q.set("severity", params.severity);
  if (params.limit) q.set("limit", String(params.limit));
  return withQuery("/api/alerts", q);
}
