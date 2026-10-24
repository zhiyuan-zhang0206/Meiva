import type { TaskFields } from "../../../contracts/types";
import { withQuery } from "./route-query";

export function inspectStatisticsPath(agentId: number, hours?: number | null): string {
  const params = new URLSearchParams();
  if (hours != null) params.set("hours", String(hours));
  return withQuery(`/api/agents/${agentId}/inspect/statistics`, params);
}

export function agentShellPath(agentId: number, sessionId: number, lines?: number): string {
  const qs = lines != null ? `?lines=${lines}` : "";
  return `/api/agents/${agentId}/shell/${sessionId}${qs}`;
}

export function commandsPath(agentId?: number | null): string {
  const qs = agentId != null ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  return `/api/commands${qs}`;
}

export function listAgentsPath(options: {
  scope?: "live" | "terminated" | "all";
  query?: string;
  beforeId?: number;
  limit?: number;
}): string {
  const params = new URLSearchParams({ scope: options.scope ?? "live", limit: String(options.limit ?? 100) });
  if (options.query) params.set("query", options.query);
  if (options.beforeId != null) params.set("before_id", String(options.beforeId));
  return `/api/agents?${params.toString()}`;
}

export function toUserPath(agentId: number, references: readonly string[]): string {
  const query = new URLSearchParams(references.map((reference) => ["reference", reference]));
  return `/api/agents/${agentId}/to-user${query.size ? `?${query.toString()}` : ""}`;
}

export function tasksPath(opts?: { window?: string; fields?: TaskFields }): string {
  const params = new URLSearchParams();
  if (opts?.window != null) params.set("window", opts.window);
  if (opts?.fields != null) params.set("fields", opts.fields);
  return withQuery("/api/tasks", params);
}
