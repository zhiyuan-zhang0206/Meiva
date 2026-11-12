# meiva.ui — User Interface

## What it is

`meiva.ui` lets agents publish results and ask users questions. Each operation
resolves the caller through the SDK context entry before sending a write. In a
bridge-launched external shell, the first UI call attaches the shell's registered
identity just as an agents or tasks call does; a failed admission remains an error.

## User delivery in SDK mode

`user_delivery_mode=sdk` routes formal communication through core SDK calls:

- `publish(content, *, suggestions=None) -> int` appends a durable
  publication. Optional suggestions supply candidate replies to the independent
  composer service. Omit them when no reply is useful.
- `ask(questions) -> list[int]` appends one question per item (`title`, optional `content`,
  `type` text / single_select / multi_select, `options`, optional `note_suggestion`) and
  returns each question's global ID in the order given. Text questions have one answer field;
  choices keep an optional Note. `note_suggestion` offers light candidate text for that field
  until the user explicitly applies it. The user answers all open questions together, each
  answered or skipped.
- `close_question(question_id) -> None` closes one of the agent's own unanswered
  questions (`resolution='withdrawn'`): its notification is withdrawn, the user no
  longer sees it as awaiting an answer, and nothing is delivered to the agent. A
  closed question closes again quietly; a missing or foreign id raises
  `LookupError`; one the user already answered or dismissed raises `ValueError`.
  A question cannot be edited.

Any number of questions may be open at once, from one call or several. The user's reply
arrives as one message with a `Re: question #<id>` block per question. There is no
read entry for publications or questions: the runtime shows the agent its
unanswered questions itself.

The runtime owns operation identity for SDK writes and deterministic replay.
The gateway refuses changed content under an accepted key. New Ask records do
not expire automatically. Their content and answers remain durable.

Publishing or asking during work never ends the turn. Ordinary assistant text,
including final summaries, stays in expandable Details. At natural completion,
the runtime checks that the turn published or asked at least once; it gives one
corrective retry if neither happened. `publish(None)`
explicitly records that no user update is needed.

Publications carry unread state; Ask carries awaiting-answer state. Reading is
not answering, and closing without an answer grants no authorization. Each ask
sends a system notification, withdrawn when the question is answered or closed.
Rewriting `to_user.md` cannot overwrite
publications or pending questions; it remains an editable overview.

The independent composer service combines agent candidates, explicit suggestion
feedback and cross-agent Memory. Suggestions remain editable and send only on
user submission. See [Reply Suggestion](../../gateway/agents/docs/reply-suggestion/reply-suggestion.md).

The other modes are `naive` (ordinary assistant text) and `prefix` (explicit
visibility markers, the compatibility default). Ask is available in every mode;
publish is available in SDK mode. Mode is frozen per agent and follows the
ordinary config/restart lifecycle.

## Key Dependencies

- [Frontend UI](../../ui/web/src/docs/frontend.md) — Timeline, publications and questions.
- [Numbered questions](../../meiva_builtins/plugins/meiva_fleet/docs/questions.md) — question answers.
