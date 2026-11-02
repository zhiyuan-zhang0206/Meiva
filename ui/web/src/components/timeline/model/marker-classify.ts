import { NOTE_TAGS } from "@/lib/contracts/constants-generated";

export type NoteTag = (typeof NOTE_TAGS)[number];

const NOTE_TAG_SET: ReadonlySet<string> = new Set(NOTE_TAGS);

function isNoteTag(s: string): s is NoteTag {
  return NOTE_TAG_SET.has(s);
}

export const LIFECYCLE_TAGS = [
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const satisfies readonly NoteTag[];

export type LifecycleTag = (typeof LIFECYCLE_TAGS)[number];

function isLifecycleTag(s: string): s is LifecycleTag {
  return (LIFECYCLE_TAGS as readonly string[]).includes(s);
}

// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.meiva.okf.md
export const MEMORY_SOURCES: ReadonlySet<NoteTag> = new Set<NoteTag>([
  "memory",
  "agent_memory",
  "inherited_memory",
]);

// meiva:note
// Notes the agent reads but the conversation timeline never shows: a recency anchor
// beside the waking inbound is guidance for the model, not an event for the reader.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.meiva.okf.md
const SILENT_NOTE_TAGS: ReadonlySet<NoteTag> = new Set<NoteTag>(["non_human_wake"]);

export type MarkerClass =
  | { readonly kind: "lifecycle"; readonly tag: LifecycleTag }
  | { readonly kind: "memory" }
  | { readonly kind: "note" }
  | { readonly kind: "ephemeral" };

// meiva:note
// Every NoteTag has a card: lifecycle and memory tags have their own, the rest are notes.
// The vocabulary is generated from the backend enum, so only a source this build does
// not know (an untagged message, or a tag newer than the page) falls to ephemeral.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.meiva.okf.md
export function classifyMarker(source: string | null): MarkerClass {
  if (source === null || !isNoteTag(source)) return { kind: "ephemeral" };
  if (isLifecycleTag(source)) return { kind: "lifecycle", tag: source };
  if (MEMORY_SOURCES.has(source)) return { kind: "memory" };
  return { kind: "note" };
}

export function isSilentEphemeralPayload(payload: string): boolean {
  return (
    payload === "compact_done" ||
    payload === "cancelled" ||
    payload.startsWith("compact_request:")
  );
}

export function isSilentMarker(source: string | null, payload: string): boolean {
  if (source !== null && isNoteTag(source)) return SILENT_NOTE_TAGS.has(source);
  return isSilentEphemeralPayload(payload);
}
