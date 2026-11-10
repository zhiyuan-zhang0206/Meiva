# meiva.ui — User Interface

## What it is

`meiva.ui` lets agents publish results and ask users questions.

## User delivery in SDK mode

`user_delivery_mode=sdk` routes formal communication through core SDK calls:

- `publish(content, *, suggestions=None) -> int` appends a durable
  publication. Optional suggestions supply candidate replies to the independent
  composer service. Omit them when no reply is useful.
- `ask(title, content=None, *, question=None, note_suggestion=None) -> int` appends a question and returns its global ID. Omitted
  question means free text; `single_select` and `multi_select` accept declared
  options. Text questions have one answer field; choices keep an optional Note.
  `note_suggestion` offers light candidate text for that field until the user
  explicitly applies it.
- `publications(pending_only=True, after_id=0, limit=50)` reads durable messages
  in increasing ID order without marking them read; continue after the last ID.
- `questions()` reads up to 500 open questions without answering them.

The runtime owns operation identity for SDK writes and deterministic replay.
The gateway refuses changed content under an accepted key. Several questions
may remain open concurrently. New Ask
records do not expire automatically. Their content and answers remain durable.

Publishing or asking during work never ends the turn. Ordinary assistant text,
including final summaries, stays in expandable Details. At natural completion,
the runtime checks that the turn published or asked at least once; it gives one
corrective retry if neither happened. `publish(None)`
explicitly records that no user update is needed.

Publications carry unread state; Ask carries awaiting-answer state. Reading is
not answering, and closing without an answer grants no authorization. Neither
call sends an additional notification. Rewriting `to_user.md` cannot overwrite
publications or pending questions; it remains an editable overview.

The independent composer service combines agent candidates, explicit suggestion
feedback and cross-agent Memory. Suggestions remain editable and send only on
user submission. See [Reply Suggestion](../../gateway/agents/docs/reply-suggestion/reply-suggestion.md).

The other modes are `naive` (ordinary assistant text) and `prefix` (explicit
visibility markers, the compatibility default). Ask is available in every mode;
publish is available in SDK mode. Mode is frozen per agent and follows the
ordinary config/restart lifecycle.

## Compatibility

The agent-facing `notify`, `edit_notice` and `dismiss_notice` entries are removed.
Ask uses the existing question store and answer receipts, preserving historical
question IDs and answers. Pending legacy questions are migrated without expiry.
Internal legacy routes remain for existing producers; SDK mode refuses new
legacy notice creation. System-alert delivery remains a separate capability.

## Key Dependencies

- [Frontend UI](../../ui/web/src/docs/frontend.md) — Timeline, publications and questions.
- [Numbered Notice Questions](../../meiva_builtins/plugins/meiva_fleet/docs/notice-questions.md) — question answers.
