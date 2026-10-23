export const LIFECYCLE_TAGS = [
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const;

export type LifecycleTag = (typeof LIFECYCLE_TAGS)[number];

function isLifecycleTag(s: string): s is LifecycleTag {
  return (LIFECYCLE_TAGS as readonly string[]).includes(s);
}

// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.meiva.okf.md
export const MEMORY_SOURCES = new Set(["memory", "agent_memory", "inherited_memory"]);

export type MarkerClass =
  | { readonly kind: "lifecycle"; readonly tag: LifecycleTag }
  | { readonly kind: "memory" }
  | { readonly kind: "note" }
  | { readonly kind: "ephemeral" };

export const NOTE_SOURCES = new Set([
  "impersonation",
  "sdk_hint",
  "memory_write_check",
  "to_user_soft_limit",
  "agent_reply",
  "task",
  "compact_reminder",
  "history_dump",
  "silent_idle_continue",
  "heartbeat",
  "security",
  "context",
  "agent_id",
  "exec_timeout",
  "timezone",
  "project_skills",
  "preloaded_skills",
  "new_skills",
  "skill_recall",
]);

export function classifyMarker(source: string | null): MarkerClass {
  if (source !== null && isLifecycleTag(source)) return { kind: "lifecycle", tag: source };
  if (source !== null && MEMORY_SOURCES.has(source)) return { kind: "memory" };
  if (source !== null && NOTE_SOURCES.has(source)) return { kind: "note" };
  return { kind: "ephemeral" };
}
