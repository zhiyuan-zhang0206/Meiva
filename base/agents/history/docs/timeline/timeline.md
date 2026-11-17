# Timeline rendering

`build_timeline_items` renders `state.messages` into `TimelineItem` items. The
agent (from in-memory state at each node enter) and the gateway
(`GET /api/agents/{id}/timeline`, from the checkpoint) call the same rule; the
module sits in `base/` (leaf) so both import it without an agent <-> gateway
cycle. Building the snapshot in the agent removed the race where the gateway
re-read the checkpoint before an async commit landed and missed the
just-claimed inbound. `TimelineItem` lives in `timeline_item.py` (file line
budget); this module re-exports it.

Inbound and attach messages render text only: a historical multimodal inbound's
base64 image blocks are dropped (thumbnails ride on `meiva_image_urls`), while
delivered upload batches (`system:upload`) carry structured file metadata on
`meiva_uploaded_files` (projected as `TimelineItem.files`); an attach's
data-URI images ride on `images` with per-image captions. The system prompt is
item 0, `created_at=None`. A compact item prefixes the payload with the
wall-clock ts when the timestamp policy is on and carries the producing run as
`compact_id`. Signature-only / redacted thinking blocks render nothing. The note timestamp split is its own node: [Note timestamp policy](note-timestamp-policy.md).

## Tool-call block offset

Code items come from the normalized tool_calls view — one provider-agnostic rule (anthropic tool_use content blocks; gemini / openai tool_calls-only shapes). block_idx = <distinct text/thinking block indices> + <ordinal in tool_calls>: a set, so a signature-only thinking block sharing a sibling's index does not inflate the offset. The streaming side computes the same offset (agent/graph/_callbacks.py); SSE ids equal committed snapshot ids.

## User-facing polarity (`user_facing`)

Every `TimelineItem` carries `user_facing`. For new `agent_chat` items,
`ai_message_items` uses `meiva_explicit_visibility` to select the first-line
visibility protocol in `base.agents.messages.reply_intent`. The projection
strips valid markers, preserves malformed text in Details, and keeps block
indices stable. Older checkpoints without that flag use their wake-based
projection. Reply suggestions use only user-facing chat.

## Reasoning timing

Each thinking block carries its own per-block duration (meiva_reasoning_ms_by_block, keyed by block_idx); the turn-level reasoning tokens attach to the FIRST thinking item only; historical messages keep None rather than an invented value. Provider-native reasoning is folded to canonical thinking blocks on a copy — the stored message stays native.

## Attach caption pairing

Each image's caption is the text block immediately before it (modern interleaved attach pack) — structural pairing, no text parsing. Legacy attach messages (one caption block then all images) cannot recover per-image lines and fall back to the legacy all-text-then-all-images layout. Only data: URLs are exposed; other media never leaks into payload or images.

## Human-message fallback

Tagged framework HumanMessages take explicit branches; anything left — pre-tag history, retired marker types like the old LLM-cancel — renders as a system_marker with source=None, which the frontend flags as the red UnknownMarkerChip. Fail-loud is deliberate: a red chip on rare historical rows beats a silent mis-render; an untagged system note is the same case.

## External harness record projection (`activity_pending`)

External harness timelines are projected on demand from each CLI's session record
(`base/agents/history/transcript_timeline/`, ids `h<offset>.<n>`; see
[reading a record](../../../../lm/harness/docs/transcript-projection.md)). An unanswered
tool call in an open turn carries `activity_pending=True` on its `agent_code` item until
its result lands in the record (`activity_pending=None`). The gateway clears
`activity_pending` when the agent status, re-read after the record RPC returns, is `idling` or
`terminated`: an aborted transcript can end with an unmatched tool call forever.
Running agents retain the record's live activity flags across Claude Code,
Codex and Antigravity. The reply-suggestion excerpt
consumes only completed user-facing chat blocks.

## Window unit and readers

display.timeline_default_limit (MEIVA_TIMELINE_DEFAULT_LIMIT, default 50) counts timeline ITEMS and is read per call through timeline_default_limit() — both the gateway and the agent import this module, so consumers must not capture the value at import. tail_window returns the newest limit items plus has_more; limit <= 0 or a list no longer than the limit returns everything with has_more=False.


## Source-qualified reading windows

Cold readers attach an optional `reading_locator` to items whose source can be
verified. Live node projection does no additional database read to create this
metadata. A locator is an opaque, versioned transport coordinate; authorization,
source availability and exact membership are checked again when it is used.

`GET /api/agents/{id}/timeline/window` requires exactly one `anchor`, `before`
or `after` token and accepts `limit` from 1 through 1000. Anchor success contains
the exact canonical target. Pages contain complete source rows, so a multi-block
row can exceed the item limit. Source-issued `older_cursor` and `newer_cursor`
advance by rows, and a proven end returns a null continuation. The navigation
window excludes the standing system prompt; ordinary head rendering keeps it.
The response's `reading_source` identifies the source view, including a verified
empty view, without asserting that a browser cache is empty or current.

Native readers obtain messages, compact boundaries and source domain from one
read-only repeatable-read checkpoint view. The epoch selects a current or retained
domain; message identity or embedded inbound identity plus block then matches
exactly one item. Local display indices never establish native membership.
A just-sealed boundary that is still the latest committed checkpoint remains the
active readable source until the next current checkpoint commits. Other retained
segments obey the configured history depth. Delta channels use the existing saver
reconstruction. Adjacent pages load one neighboring segment and return explicit
unavailability if an intermediate segment cannot provide a stable edge.

Record readers bind the agent's current registry, harness, session, committed row
and block. They share the existing parser, projector, complete-row anchor and
call context with ordinary and Quote reads. The gateway awaits the record RPC,
then calls the single `settle_record_activity` owner; an inactive process settles
activity without completing partial source text.

Malformed selectors return 422. A verified unavailable identity, source domain
or read budget returns 410 with code `reading_source_unavailable` and a
`reading_reason` extension (`identity`, `source`, or `budget`). Transport and
source failures keep the existing 503/502 behavior. Record dispatch is bounded
to three seconds with no retry. A reader without selector support cannot report
its head as a successful anchored response. Ordinary requests omit unused new
selector fields, and ordinary RPC replies omit unused new envelope fields.

Browser restoration is a separate consumer stage. A failed or cancelled read,
budget refusal or unavailable source is not evidence that a saved bookmark was
deleted and must not cause a fallback viewport to overwrite it.
