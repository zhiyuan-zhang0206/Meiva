import { withQuery } from "./route-query";

export type ReadingWindowSelector =
  | { anchor: string; before?: never; after?: never }
  | { before: string; anchor?: never; after?: never }
  | { after: string; anchor?: never; before?: never };

export function readingWindowPath(agentId: number, selector: ReadingWindowSelector, limit?: number): string {
  const params = new URLSearchParams();
  for (const key of ["anchor", "before", "after"] as const) {
    const value = selector[key];
    if (value !== undefined) params.set(key, value);
  }
  if (limit !== undefined) params.set("limit", String(limit));
  return withQuery(`/api/agents/${agentId}/timeline/window`, params);
}
