import { withQuery } from "./route-query";

export interface RunTimelineWindow {
  from?: string;
  to?: string;
  recentHours?: number;
  maxUnits?: number;
  snapshot?: string;
}

export function timelinePath(agentId: number, opts?: { limit?: number; before?: string }): string {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.before != null) params.set("before", opts.before);
  return withQuery(`/api/agents/${agentId}/timeline`, params);
}

export function runTimelinePath(agentId: number, options?: RunTimelineWindow): string {
  const params = new URLSearchParams();
  if (options?.from != null) params.set("from", options.from);
  if (options?.to != null) params.set("to", options.to);
  if (options?.recentHours != null) params.set("recent_hours", String(options.recentHours));
  if (options?.maxUnits != null) params.set("max_units", String(options.maxUnits));
  if (options?.snapshot != null) params.set("snapshot", options.snapshot);
  return withQuery(`/api/agents/${agentId}/run-timeline`, params);
}

export function runTimelineLinksPath(
  agents: readonly number[],
  timeWindow: { from: string; to: string },
): string {
  const params = new URLSearchParams({ agents: agents.join(","), from: timeWindow.from, to: timeWindow.to });
  return `/api/insights/run-timeline/links?${params.toString()}`;
}

export function runTimelineLinkContentPath(ref: { inbound_id: number } | { notice_id: number }): string {
  const params = new URLSearchParams(Object.entries(ref).map(([k, v]) => [k, String(v)]));
  return `/api/insights/run-timeline/link-content?${params.toString()}`;
}

export function runTimelineContextPath(agentId: number, at: number, snapshot?: string): string {
  const params = new URLSearchParams({ at: String(at) });
  if (snapshot !== undefined) params.set("snapshot", snapshot);
  return `/api/agents/${agentId}/run-timeline/context?${params.toString()}`;
}

export function runTimelineMessagesPath(
  agentId: number,
  range: { start: number; end: number; limit?: number; full?: boolean; snapshot?: string },
): string {
  const params = new URLSearchParams({ start: String(range.start), end: String(range.end) });
  if (range.limit != null) params.set("limit", String(range.limit));
  if (range.full) params.set("full", "true");
  if (range.snapshot !== undefined) params.set("snapshot", range.snapshot);
  return `/api/agents/${agentId}/run-timeline/messages?${params.toString()}`;
}
