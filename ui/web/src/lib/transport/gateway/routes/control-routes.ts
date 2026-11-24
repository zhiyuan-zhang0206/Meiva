import { withQuery } from "./route-query";

export function agentQuestionsPath(agentId: number): string {
  return withQuery("/api/questions/open", new URLSearchParams({ agent_id: String(agentId) }));
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
