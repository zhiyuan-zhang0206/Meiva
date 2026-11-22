# System markers

`components/timeline/model/marker-classify.ts` classifies every rendered `system_marker`
item by its `source` and `payload` into collapsible `MessageCard` families —
`background_task` (`background_task` / `task-notification`), `task`, lifecycle
transitions, memory recall, guidance notes, `system_notice` (`status:*`), `error`
(`error:*`), and `unrecognized` — so no status line, task return, or receipt ever
dumps bare text into the timeline (user ruling 2026-11-22). Every rendered marker
belongs inside a Work Block and starts collapsed (`fixedDefault: false`).
`components/timeline/markers.tsx` owns rendered marker visuals (`markerVisual`) and
bodies (`MarkerBody`, `ErrorMarkerBody`, `UnknownMarkerChip`, plus legacy direct
`EphemeralSystemMarker`). All marker families share the same borderless monochrome
`CardHeader` chrome inside a `TurnBlock` (no colored accent overrides; user
ruling 2026-11-01); titles are i18n keys under the `markers` namespace with an
English fallback.

The vocabulary is generated, not mirrored: `NOTE_TAGS` in
`lib/contracts/constants-generated.ts` comes from the backend `NoteTag` enum
(`scripts/codegen/dump_frontend_constants.py`, kept fresh by the
`constants-codegen-fresh` hook). `LIFECYCLE_TAGS`, `MEMORY_SOURCES`, and `"task"`
are typed subsets of it, and every other member is a note, so a new backend tag
renders as a generic note with no frontend change. A frontend test iterating
`NOTE_TAGS` locks every member to its card and never the unrecognized alarm.
`isSilentMarker` owns which markers the conversation timeline hides, and it hides
nothing that carries a `NoteTag`: every note the agent read renders, including the
`non_human_wake` recency reminder and `reply_visibility` (user ruling 2026-11-09,
replacing the silent filter those two used to have). Only the ephemeral bookkeeping
payloads below are silent, and a recognised tag is never judged by its payload — a
note whose body reads like machinery still renders.

Non-`NoteTag` markers inside the Work Block follow one card-based dispatch:
`compact_done` and `compact_request:*` are normally filtered at the SSE layer
(`applySystemEvent`), so their branches only catch stale rows; `cancelled` and
`status:Cancellation requested.` (legacy rows) are silent (the stop button has no
UI noise); `source === "background_task" | "task-notification"` renders a
`Background task` (`Terminal`) card whose expanded body strips a leading `status:`
prefix via `stripStatusPrefix`; `payload.startsWith("error:")` renders an `Error`
(`AlertTriangle`) card whose expanded body mounts `ErrorMarkerBody` (`data-testid="marker-error"`
and `data-quote-body`); any other `payload.startsWith("status:")` (including
`scheduled_wake`, `queued_input_revived`, `unknown`, `null`, or any unknown source)
renders a `System notice` (`Info`) card whose expanded body strips `status:`;
anything else renders an `Unrecognized system_marker` (`AlertTriangle`) card whose
expanded body mounts `UnknownMarkerChip` (`data-testid="marker-unrecognized"`,
`data-quote-body` on its payload `<pre>` only) with the raw `source`/`payload`
shown so the missing case is obvious. Because the vocabulary is generated, a
non-null source the page does not know is a tag newer than the loaded bundle (a tab
opened before a deploy), so `UnknownMarkerChip` also asks the reader to reload
(`data-testid="marker-unrecognized-reload"`); a null source is an untagged message,
which a reload cannot fix, and gets no such hint. The alarm also logs to the dev
console — the timeline is client-only `"use client"` code, so no `window` guard is
needed.

`ErrorMarkerBody`, `UnknownMarkerChip`, and `EphemeralSystemMarker` preserve the
stable assertion hooks: `marker-unrecognized` must never render for a page built
from the running server's code, `marker-unrecognized-reload` marks the stale-page
hint, and `marker-error` is the legit error path; absence of the alarm node in an
e2e run means the #1017 alarm class did not render.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the item list that hosts system markers.
- [Message Metadata Contract](../../../../../../base/agents/messages/docs/kwargs.md) — the backend NoteTag closed set `NOTE_TAGS` is generated from.
