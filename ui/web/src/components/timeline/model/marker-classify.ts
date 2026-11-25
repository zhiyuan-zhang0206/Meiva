import { NOTE_TAGS } from "@/lib/contracts/constants-generated";

export type NoteTag = (typeof NOTE_TAGS)[number];

const NOTE_TAG_SET: ReadonlySet<string> = new Set(NOTE_TAGS);
const ARCHIVED_TASK_NOTE = "task";
export const HARNESS_SWITCH_SOURCE = "harness_switch";

function isNoteTag(s: string): s is NoteTag {
  return NOTE_TAG_SET.has(s);
}

export const LIFECYCLE_TAGS = [
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_interrupt",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const satisfies readonly NoteTag[];

export type LifecycleTag = (typeof LIFECYCLE_TAGS)[number];

function isLifecycleTag(s: string): s is LifecycleTag {
  return (LIFECYCLE_TAGS as readonly string[]).includes(s);
}

// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.md
export const MEMORY_SOURCES: ReadonlySet<NoteTag> = new Set<NoteTag>([
  "memory",
  "agent_memory",
  "inherited_memory",
]);

export type MarkerClass =
  | { readonly kind: "lifecycle"; readonly tag: LifecycleTag }
  | { readonly kind: "harness_switch" }
  | { readonly kind: "memory" }
  | { readonly kind: "background_task" }
  | { readonly kind: "note" }
  | { readonly kind: "system_notice" }
  | { readonly kind: "error" }
  | { readonly kind: "unrecognized" };

// meiva:note
// Every rendered system_marker maps to a collapsible card family: background_task /
// task-notification, NoteTag families (lifecycle, memory, note), payload-typed
// error: / status: notices, and an unrecognized fallback card so no status or receipt
// dumps bare text into the timeline.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.md
export function classifyMarker(source: string | null, payload?: string): MarkerClass {
  if (source === "background_task" || source === "task-notification") {
    return { kind: "background_task" };
  }
  if (source === HARNESS_SWITCH_SOURCE) return { kind: "harness_switch" };
  if (source === ARCHIVED_TASK_NOTE) return { kind: "note" };
  if (source !== null && isNoteTag(source)) {
    if (isLifecycleTag(source)) return { kind: "lifecycle", tag: source };
    if (MEMORY_SOURCES.has(source)) return { kind: "memory" };
    return { kind: "note" };
  }
  if (payload?.startsWith("error:")) return { kind: "error" };
  if (payload?.startsWith("status:")) return { kind: "system_notice" };
  return { kind: "unrecognized" };
}

export function stripStatusPrefix(payload: string): string {
  return payload.startsWith("status:") ? payload.slice("status:".length) : payload;
}

export function isSilentEphemeralPayload(payload: string): boolean {
  return (
    payload === "compact_done" ||
    payload === "cancelled" ||
    payload === "status:Cancellation requested." ||
    payload.startsWith("compact_request:")
  );
}

// meiva:note
// Nothing the agent reads is filtered out of the timeline any more: every NoteTag
// renders, including the wake and visibility notes that used to be swallowed
// (user ruling 2026-11-09). Only the ephemeral bookkeeping payloads stay hidden -
// a compact that already happened, a cancelled run, a compact request - because
// those are markers of machinery, not content the agent read. A recognised
// NoteTag is never judged by its payload, so a note whose body happens to read
// like machinery still renders.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.md
export function isSilentMarker(source: string | null, payload: string): boolean {
  if (
    source === ARCHIVED_TASK_NOTE ||
    source === HARNESS_SWITCH_SOURCE ||
    (source !== null && isNoteTag(source))
  ) {
    return false;
  }
  return isSilentEphemeralPayload(payload);
}
