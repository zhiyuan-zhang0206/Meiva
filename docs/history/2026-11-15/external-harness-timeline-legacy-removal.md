# Remove the legacy external harness database timeline path

Following the verified rollout of [Antigravity record projection](../2026-11-13/antigravity-record-projection.md), `reads_timeline_from_record` covers every external harness (Claude Code, Codex, and Antigravity), fulfilling Step 2 of [the record-read timeline decision](../../decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md). This change deletes the superseded event-stream-to-database timeline path across storage, runtime, wire contracts, and frontend state.

## Removed legacy paths

- **Database and grants**: `agent_harness_messages` is dropped via forward migration `20261114T165000_drop-agent-harness-messages.sql` and removed from `db/schema.sql` (with folded prior `ALTER TABLE` migrations stamped in `schema_migrations`), role grant matrices, and migration smoke checks.
- **Projector, store, and readers**: `base/agents/history/harness_messages.py` (`HarnessTurnProjector`, `append_harness_items`, `replace_harness_item`, `query_harness_timeline`, `copy_harness_timeline`, `RUNNING_REVISION`), `TurnSink`/`NullTurnSink`, and all `agent_harness_messages` reader, fallback, birth-copy, switch-note, and lost-turn branches are removed.
- **Wire and session residue**: `TimelineItem.activity_revision`, `TimelineItem.parent_item_id`, `TimelineItem.tool_use_id`, the `"subagent_text"` item kind, `"harness_messages"` / `"harness_transcript"` timeline source literals, session/adapter sub-agent staging, frontend child-nesting and revision-merge branches, and the unreferenced Antigravity `transcript.json` row parser (`parse.py`, `fields.py`, and its fixtures) are deleted.

## Retained boundaries

- **Record-read and native paths**: External harness timelines are projected on demand from each CLI's own on-disk record (`.jsonl` / rollout / `.pb`) with `ReadTimelineSink` (`timeline_dirty` wakeups); built-in Meiva agents continue to read LangGraph checkpoints.
- **Pending call timing**: `TimelineDraft.tool_use_id` remains for same-turn call/result pairing, and `TimelineItem.activity_pending` remains exclusively for unanswered external `agent_code` calls. Frontend `clearCodeClocks` strips `activity_pending` and stamps `codeElapsedMs` on `settleTurn` so idle-turn settlement stays idempotent.
- **Live sub-agent stream isolation**: Live adapter `SubagentText` events and `parent_tool_use_id` in `base/lm/harness/events.py` stay so sub-agent output is never misclassified as `AssistantText` while still marking `ReadTimelineSink` dirty.

## Rejected alternatives and unstarted follow-up

Retaining a dual-read fallback, compatibility toggle, or dormant `agent_harness_messages` table was rejected: once every external harness reads from its CLI record, keeping a second path would invite drift and accidental reads or writes. Historical rows in `agent_harness_messages` predating record projection are intentionally dropped under the lossy deployment precondition that running gateway and runner processes already serve from records before the update; production deployment remains a separately authorized operator step.

Step 3 — reading sub-agent records on demand at timeline read time in place of the retired stored `subagent_text` / `parent_item_id` tree — is not started in this change and remains tracked in [the transcript timeline plan](../../../future/agent/transcript-timeline-source.md).
